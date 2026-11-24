"""``plan_dispatch`` job_type — the dark-factory tick that drives plan subtrees.

Spec: `me692430` § "Plans" → "The dispatcher tick" and
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
   ``release.round`` names an open round on it (td573809, me573231). A
   ``sha-in-prod`` gate that opens under a root with ``meta.source_todo``
   also closes the chain's source todo
   (:func:`precis.plans.todo_chain.close_source`, :func:`_close_chain_source`):
   a failure is noted and logged, never fails the gate.
2. **loops** — once every current child of a loop is closed: ``until`` open
   or ``meta.loop.count`` (default 0) at ``max`` closes the loop; otherwise
   the body is re-minted under it (:func:`precis.plans.templates.mint_body`)
   and the count bumped. An open informational ask (``meta.ask.blocks ==
   'nothing'``) counts as closed here and in step 3: it is a record for the
   human, not a child still at work (gr589823). A ``waiting-for:`` tag on
   the loop itself parks it outright — no re-mint, no close (td610546).
   A body with no minter closes the loop with a comment naming the gap.
   Release fixes continue on each red verdict until the release is green.
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
   between the line start and the token) or right after boundary
   punctuation (``.!?``, plus the ``:``/``)`` a glued text block leaves
   before the verdict, gr687501), outside backticks/parens — the
   verdict lines too, so a
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
   result. A LAND on a held pre-land land mints the held ``land_slice``
   only when the verdict's judged head (``verified head <sha>``,
   :func:`_verdict_heads`) is the stamp's pushed tip — a LAND naming
   another head read a different commit than the hold would ship, so
   the hold stands and the reviewer re-runs, retry-capped like a
   malformed verdict (td730820). ``LAND`` closes the node. ``FIX`` mints
   one ``run_slice`` task
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
   complete findings and landed shas for one briefed builder. A FIX or
   REVERT on a branch a pre-land reviewer still holds (gr570506) mints
   nothing: the build reopens with the finding as its ``handoff:`` and
   the reviewer stays open for the rebuild. The send-backs are counted
   in ``meta.preland_fix_rounds`` — the finding's digest and
   ``anchors:``-line ``file::function`` set plus the pushed head the
   verdict judged —
   and the build is held on an ``ask-coordinator`` ask only on the
   failure the ask describes (gr688058): the reviewed head unchanged
   since the last send-back (the builder is not acting), the head the
   verdict judged other than the pushed tip (the reviewer is reading a
   stale branch), the newest finding the last one's again (the same
   text, or a shared ``file::function`` on the verdict's ``anchors:``
   line with token-Jaccard text similarity of at least 0.6), or
   :data:`PRELAND_FIX_CAP` rounds in a
   row (gr684815). The ask resets only the count (gr762396): the head,
   digest, anchors and text stay, so the first round after the answer
   still stops on an unchanged head or the same finding, and a held land
   whose head is the one the last send-back rejected (a session that
   pushed nothing re-held it) never releases the reviewer — the build
   reopens and parks on the "unchanged head" stop instead — unless a
   coordinator ``brief:`` on the reviewer or the builder's ``validated:``
   rebuttal, newer than the last rejection, buys that head one fresh
   review (a new finding from a brief-bought review is an ordinary
   send-back), or the gating review is not the one that rejected it.
   An open pre-land stop ask blocks both the build and its review, even
   when the worker marks the build done. A no-op rebuild on a denied
   finding parks before another review. A single-target review reporting
   duplicate work already on main closes through the task moot path.
   ``REVERT``
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
   td604695). An unverified or hand-posted moot records
   ``<verdict> — plan dropped`` on the gripe and cools its dispatch
   (gr759153). A ``brief`` generator is exempt from all of it.
2b. **todo briefs** (:func:`_step_todo_briefs`) — a ``brief`` generator
   under a root carrying ``meta.source_todo`` whose newest job succeeded:
   the ``handoff:`` comment that job posted (its chunk ``meta`` binds it
   to the job — a later ``handoff:`` comment, whoever wrote it, decides
   nothing) is parsed by
   :func:`precis.plans.todo_chain.parse_brief` (first line only). A
   ``brief: td<build>`` naming the chain's build node, with a ``Read:``
   line naming the source revision the writer read, is posted on it as a
   ``brief:`` comment — carrying that stamp — and the brief node closes
   done, in one transaction; a ``refuse: <reason> — <why>`` files the one
   ask on the source todo, tags it ``needs-spec`` and closes the chain
   won't-do with no build; a ``split: <n>`` of two to four well-formed
   parts creates the part todos under the source (``split_source``) and
   closes the chain the same way with no build and no ask (more than four
   parts, or a source that is itself a part, is a ``split`` refusal
   instead); a ``refuse: built`` whose source is open, unheld and without
   an open child closes the source done on the session's evidence
   (``close_built``), the chain the same way with no ask; anything else
   reopens the node as a failed attempt (the
   succeeded job counts in :func:`_attempts`, so the retry cap, rescue
   and ask are the ordinary ones). A build under such a root with no
   ``brief:`` comment never dispatches, whatever its blockers say, and
   one whose newest ``brief:`` stamp the live source has moved past
   re-briefs instead of building (:func:`_rebrief_stale_brief`).
3. **phases** — first reopens a generator that is ``STATUS:done`` while a
   child is still open (a generator's session mints children, it does not
   close the node; counted as ``reopened``). Then closes every node whose work
   is finished: a phase
   (``meta.auto_close``) or a generator with children, once its subtree
   has settled — unless the node itself carries a ``waiting-for:`` tag, a park
   that outranks its children's state (the FIX-chain cap's
   ``waiting-for:coordinator`` on the root, an unsettled mint); an
   ``analyst`` generator once its newest job succeeded and an
   ``analysis:`` comment was posted since (td543749); a mechanical task (``meta.op`` other than ``run_slice``) whose
   newest job succeeded; a ``run_slice`` task whose newest job is a
   succeeded ``land_slice`` (a succeeded build alone never closes it, and
   a build plus its red land count as one attempt); a ``pin_bump`` node
   closes only once its record's ``image.ready`` says the land host's gate
   image carries the pin (td659522 F7, Reto 2026-11-15; its job's image
   wait stamps it and closes the node, this is the backstop). Its job hands
   a branch to ``land_slice`` and succeeds, so a succeeded ``pin_bump``
   alone neither closes the node nor re-dispatches it, and a failed land
   re-dispatches the build; a succeeded land is no attempt, and the next
   tick mints the image wait, pinned to the land's ``target_node``.
   Deepest first, so nested phases cascade in one pass.
4. **dispatch** — each doable task (``node_kind`` task, a generator with no
   children yet, or a plain leaf under a plan root; ``STATUS:open``, no live
   ``blocked-by`` target on it or an ancestor — a terminal target stays
   live while its ``meta.pending_land`` holds, the dependant's clone
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
   A ``pin_bump`` node (td659522 step 4) takes ``release_todo`` (its one
   ``blocked-by`` ``repo_release`` node) and ``pin_repo`` (that node's one
   non-default repo) from the plan; it is skipped, with a note, when it has
   no single such blocker, the release node governs no or several repos or
   the default one, or the node itself resolves to a repo other than precis.
   A ``run_slice`` class with no enabled offering is blocked on one
   coordinator ask before any job is minted (gr611305). So is a
   ``run_slice`` leaf whose judged text — its body and newest
   ``brief:`` — asks to write a deny path or is prod-graph only:
   work no builder lane may write skips the ladder and files one
   ``ask-coordinator`` ask with the brief attached (gr645163, Reto's
   2026-11-12 "escalate to next model" ruling — td643078/td638980's
   won't-do stalls and gr630346/gr630611's no-commit runs were this
   class). The deny-path leg is write intent, not mention (gr685970 —
   the shared :func:`precis.plans.route.coordinator_route` classifier):
   a bare mention, a test-fixture path or an ``Out of scope`` line does
   not route. Generators never classify: they read source text, and a
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
   ask offers ``land again (no builder)``. A land the host refused for
   capacity (``red_class == "admission"``) is not counted there: it rides
   the free-failure backoff and asks only after
   :data:`LAND_CAPACITY_WAIT_CAP` of unbroken refusals. **Lint reds**:
   a failed land with ruff or mypy diagnostics in its handoff gets one
   same-rung retry with the gate's error lines in the brief. Consecutive
   lint reds after the first climb the ladder with the lander's handoff.
   **Mechanical reds** (gr604579,
   factory plan 3b): a builder task's newest failed attempt whose error
   text names a known mechanical cause — a stale or colliding migration
   number, an H2 chunk over 4000 chars, a skill file over its size cap, a
   registry name in no skill, a rebase/merge conflict, or squawk
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
   gripe build, whichever root is older. The job carries the effective
   score and release claim band into host claim order. Every doable build
   queues; a build whose file set
   overlaps a started, unlanded build's in any plan — building, held for
   its pre-land review, or landing — stays queued until that one lands
   (td603849, widened 2026-11-15 — the ``_started_slices``/
   ``_overlap_inputs`` check; a started build's files are its stamped
   ``meta.branch_files`` plus the files its body, briefs and the lander's
   failure handoff name — never a builder's plain ``handoff:``,
   gr734976). "Overlaps" means the same
   repo-relative path: a brief's shorter suffix (``land_slice.py``,
   ``job_types/land_slice.py``) resolves only when exactly one of the
   started build's paths ends with it, so two builds touching different
   ``dispatch.py`` or ``__init__.py`` files do not wait on each other.
   Only builds with the same land target wait on each other. The target
   comes from metadata, body or selected brief, with main as the default.
   A build whose ``land_slice``
   died (newest job failed/cancelled/timed-out, none live) or that
   carries or sits under a ``rescue:``/``child-failed:`` tag — or whose
   pending pre-land review todo carries one — holds no lock (gr701628);
   the dead-land drop reads only a failure of the
   stamp's own ``head`` — a rebuild's fresh ``pending_land`` outlives the
   historical dead job (td701778). ``live_nodes`` counts a held
   candidate as not live.
   A reviewer's ``run_slice`` uses :func:`_review_lane` to prefer available
   vendors that left no commits on the build. A fallback to the original
   builder's vendor is noted. **Mint barrier**
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
   an open pin-bump node whose image wait found the gate image behind the
   pin (``image.reason`` stale/unlabelled/absent, or docker unreachable for
   over :data:`PIN_STALE_AFTER`) files ONE coordinator ask naming
   ``scripts/build-image precis-dev`` on that host, under the root and not
   blocking the node (deduped on :data:`PIN_IMAGE_ASK_KEY` while it is open
   for the same wanted sha; closed with the image still behind, it is asked
   again at most hourly), and an open image ask whose node is closed is
   closed with it (td659522 F7); such a wait behind for over
   :data:`PIN_STALE_AFTER` also raises the stale-open alert although its
   parked job is live, and the alert resolves itself once the node closes
   or the stale condition clears (gr764155);
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
   cannot run past it (td601015). A rung the node is *pinned* to
   (``meta.op.params.model``: a deep review fix born on the top rung, the
   release-fix owner) is bound too (gr739266): :func:`_pinned_spend`
   stamps ``meta.spend`` and mints with the remaining ``budget_usd``,
   and a pin whose cap is spent is capped like a spent ladder node
   (:func:`_spend_rungs` feeds :func:`_spend_hit` just the pinned rung).
   An uncapped rung is never spend-blocked. What
   parks it follows the failure: an
   exhausted ``run_slice`` node enters rescue —
   :func:`precis.workers.factory_rescue.enter_rescue` tags it
   ``rescue:<cls>`` (cls ``review`` for a reviewer, else ``builder``),
   files or joins the signature gripe, and the rescue pass owns what
   follows — one diagnose-and-re-arm, then its own ask on a second
   exhaustion (``meta.rescue.n >= 1``), the end of the autonomous road;
   a clean exit the quality sampling flagged (``meta.outcome.review``
   on the succeeded attempt) also mints the run-quality judge for the
   attempt (gr753725) — the expensive layer adjudicates the suspiciously
   cheap completion instead of the flag stalling on the job row. Judge
   tasks are excluded from this escalation to prevent recursive judging;
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
   :data:`FREE_RUN_CAP`, since the retry bound never sees it (gr662077).
   That includes the lander's ``"index-lock"`` wait on its own ship
   folder (:data:`INDEX_LOCK_WAIT_KEY`), which also re-lands without a
   builder (:func:`_reland_params`). An
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

6. **park stamp** (``_step_park_stamp``; park = save, resume where the
   work is, Reto 2026-11-16: parked work holds no build seat and no timer
   throws it away): whichever path parked a ``run_slice`` build — an open
   ask (hygiene's, the rescue pass's, the stall sweep's), ``rescue:*`` or
   ``child-failed:*``, ``STATUS:paused`` — its work is already kept: in
   the build host's hub, as the task's ``meta.build_ref`` (``{state, ref,
   sha, head, wip, by, note, host, at}``, stamped by the worker after a
   job pushed), or, when the push was withheld by the secret scan or
   failed, only in the attempt folder ``slice-td<todo>-j<job>`` on
   ``meta.push.host`` of the newest ended job. The step copies it to the
   node as ``meta.parked = {host, ref, sha, head, wip, state, at, job}``
   and writes host, ref and head into the body of every open ask blocking
   the node (an ask minted this pass is reached next pass — the step
   reads the plan as loaded). Answering and closing an ask re-arms the
   node (:func:`_step_rearm`), whose next ``run_slice`` continues from
   that commit on the host that holds it; an answer saying ``start over``
   stamps ``meta.start_over`` and clears ``meta.parked`` so the launch
   deletes the task's refs in that host's hub, clears ``meta.build_ref``
   and cuts from the base. ``_step_rearm`` leaves ``build_ref`` alone: the
   launch needs its host.

   **Host pin** (gr741695): a hub is local to its host, so while a task
   has ``meta.build_ref`` with a ``host``, every job that needs that work
   mints with ``params.target_node`` = that host
   (:func:`_hub_pin`, :func:`_pin_job_host`): the task's next
   ``run_slice`` build on every path (retry, re-arm, mechanical-red retry,
   escalation), a reviewer ``run_slice`` whose review target holds the
   record (or a ``meta.pending_land.target_node``), and the task's
   ``land_slice`` (the held pre-land land and a re-land). A
   ``target_node`` already in the params that names another host loses to
   the pin, with a note. A pinned job no host claims reaches the
   placement ask (:func:`_step_unplaced`), whose text then says how to
   release the pin: clear ``meta.build_ref`` and cancel the job, which
   loses the work held on that host. Not solved yet, and all must be
   before a second build host is enabled: lane choice (:func:`_escalate`,
   :func:`_review_lane`, the conflict path's try 3) does not check that
   the pinned host runs the lane it picks; a reviewer whose targets
   sit on different hosts is pinned to the first by id (with a note);
   after a start-over or a released pin, the task's attempt refs can
   remain in the other host's hub, where a launch without a record takes
   the newest; releasing a pin is a hand edit of the task's meta, not an
   answer on the ask; and the Codex sandbox can write the hub directory
   (gr742453).

   **Land conflict** (gr741695, Reto's ruling gr735593): a land red on a
   rebase or merge conflict against the base leaves ``meta.conflict =
   {head, ref, base_tip, files, at}`` on the task (the lander) and the
   task reopens with ``meta.build_ref`` standing. The next ``run_slice``
   is a conflict-only attempt — ``params.conflict = {head, ref, base_tip,
   files, try}``, ``try`` from 1 — and the mechanical ``conflict`` brief
   does not also fire (:func:`_conflict_view`, :func:`_conflict_pin`).
   Tries 1 and 2 run on the rung the build ran on, try 3 on the ladder's
   top rung; the tries are counted in ``meta.conflict_tries``, outside the
   ordinary attempt count (:func:`_attempt_jobs`) and never climb the
   ladder. A try fails when its job ends without pushed work, when the
   lander stamps the conflict again after it was minted, or when its
   resolution review says FIX (not a pre-land round: the tries are the
   bound). A third failure files one coordinator ask. The pre-land review
   of a resolution is minted with ``params.resolution = {of: <the head
   that failed to land>, ref}`` (:func:`_resolution_param`). A pushed
   resolution lands first: its reviewer and land jobs take
   :data:`CONFLICT_FIRST_SCORE` (:func:`_claim_score`,
   :func:`_step_conflict_first`), and while its land is pending the tick
   mints no other land for the same repository and base
   (:func:`_conflict_holder`) — bounded by :data:`CONFLICT_LAND_HOLD` from
   the push, after which a note says the hold lapsed. Known and accepted:
   a land minted by the builder job itself (a flow without a pre-land
   review) and a land already queued or running are not held by the tick;
   the top claim score orders them, but main can still move once under a
   pending resolution — the resolution's land rebases onto that, and a new
   conflict there is one more failed try, which the three tries absorb.
   The episode is over (:func:`_conflict_void`) once a ``land_slice``
   newer than the newest attempt has ended and the stamp was not written
   by it (``meta.conflict.job`` against the land's job id; for a stamp
   without ``job``, ``at`` against the land's mint time, ``created_at``) —
   it failed for another reason, was cancelled or timed out; a land that
   failed outside the diff (attempt-free) does not end it — or when the
   task holds no ``build_ref`` any more: the tick clears
   ``meta.conflict`` and ``meta.conflict_tries`` with a note
   (:func:`_step_conflict_clear`) and the ordinary path takes over. A
   deterministic refusal on a conflict attempt (a human-only path, a model
   rule, the coordinator route) is read from the newest ended job whatever
   path minted it, and its own ask wins over the conflict ask. A
   try 3 that cannot use the strongest rung for good (no enabled rung, its
   ``rung_caps`` cap spent) spends the tries and the ask names why; a
   backed-off lane only waits.

Plan roots: the ids named by ``plan:<id>`` tags on live todos, plus every
live todo whose meta makes it a gate / loop / generator / phase; a root
inside another root's subtree is folded into it. Closed roots are skipped.

**Kill switch.** ``PRECIS_FACTORY=off`` in the worker's env, a
``service_config`` row for ``plan_dispatch`` with ``prio = 0`` (any host), or
``params.dry_run`` makes the pass read-only: every step runs on the
in-memory model and counts what it would have done, nothing is written. The
job summary's first line is
``plan_dispatch: roots=N gates=o/c loops=minted/closed phases=closed
dispatched=N released=N asks=N dry_run=bool reopened=N reviews=N held=N``
(``held``: candidates waiting on file overlap at claim); the
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

import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import groupby
from operator import itemgetter
from typing import Any, Literal, overload

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_stale_alerts
from precis.errors import BadInput
from precis.plans import gates as _gates
from precis.plans import governs as _governs
from precis.plans import lanes as _lanes
from precis.plans import release as _release
from precis.plans import route as _route
from precis.plans import templates as _templates
from precis.plans.logbook import log_plan_event
from precis.plans.nodes import (
    DISPATCH_HOLD_KEY,
    MINT_HOLD_TAG,
    MOOT_TAG,
    any_verdict,
    finish_mint,
    generator_verdict,
    node_kind,
)
from precis.plans.todo_chain import (
    BRIEF_GRAMMAR,
    CLIP_MARKER,
    HANDOFF_JOB_KEY,
    MAX_SPLIT_PARTS,
    SOURCE_TODO_KEY,
    SPLIT_PART_KEY,
    Brief,
    Refusal,
    Split,
    brief_comment_text,
    close_built,
    close_source,
    parse_brief,
    parse_read_line,
    post_brief,
    refuse_source,
    source_updated_at,
    split_source,
)
from precis.principal import worker_comment_meta
from precis.store._todo_sql import _DOABLE_EXCLUSION_TAGS, SERVES_PARENT_WAIT_KEY
from precis.store.advisory import try_xact_advisory_lock
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta, release_claims
from precis.todo.views import _fetch_subtree
from precis.utils import dispatch_score as _dscore
from precis.workers.job_types import (
    JobTypeSpec,
    _lane_decide,
    _slice_park,
    get_job_type,
)
from precis.workers.job_types._slice_adapters import devin_retryable_agent_error

log = logging.getLogger(__name__)

NAME = "plan_dispatch"

#: Advisory-lock key serialising whole ticks (ascii "PLANTICK"; fits bigint).
#: Held as a transaction-scoped lock on a dedicated connection for the full
#: pass, so the job path and the worker-lane pass never mint or dispatch the
#: same thing twice.
TICK_LOCK_KEY: int = 0x504C414E5449434B
_STARTED_SLICES_LOCK_KEY = 0x534C4943454F564C
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

#: An open pin-bump node with no live job this long after its release node
#: tagged raises the ``pin_bump/stale-open:{todo}`` alert (td659522 step 4).
PIN_STALE_AFTER = timedelta(hours=6)
_PIN_STALE_SOURCE = RULES_BY_ID["pin_bump/stale-open:{todo}"].rule

#: ``pin_bump.RECORD_KEY`` and ``pin_bump.IMAGE_ASK_KEY``, restated (a test
#: pins the spellings equal): the pin record on the node, and the image
#: ask's dedup mark ``{"ask": id, "want": sha}`` (td659522 F7).
PIN_RECORD_KEY = "pin_bump"
PIN_IMAGE_ASK_KEY = "pin_image_ask"
#: ``image.reason`` values of a gate image behind the pin; ``unreachable``
#: docker asks only once it has lasted :data:`PIN_STALE_AFTER`.
PIN_IMAGE_STALE = frozenset({"stale", "unlabelled", "absent"})
PIN_IMAGE_UNREACHABLE = "unreachable"
#: ``pin_bump.GATE_IMAGE``: the image the gate runs in, the one the ask
#: tells the coordinator to rebuild (``scripts/build-image precis-dev``).
PIN_GATE_IMAGE = "precis-dev"
#: An image ask closed while the image still lacks the pin is asked again,
#: at most this long after the previous ask closed.
PIN_IMAGE_REASK_AFTER = timedelta(hours=1)

#: A plan job queued longer than this with no host advertising its placement
#: capabilities gets a pass note (td543917).
QUEUED_NOTE_AFTER = timedelta(minutes=10)

#: A plan job queued longer than this with no live host advertising its
#: placement capabilities also files ONE coordinator ask on its node
#: (gr657333): the sweeper's ``scheduler/unschedulable`` warn alert is
#: the standing page, but a ``run_slice`` needing
#: claude_bin/repo_checkout/git still sat queued 51+ min with no
#: capable host and nobody asked. Deduped per job on
#: :data:`_UNPLACED_ASK_KEY` (:func:`_unplaced_asked`): a closed ask is the
#: ruling for the job it was raised on and is not re-minted for it, but a
#: later job of the node that sits unplaced asks again.
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

#: Pre-land FIX/REVERT verdicts in a row on one build before the tick
#: stops rebuilding it and asks the coordinator (gr684815). Nothing
#: bounded that loop — every send-back re-arms the builder's retry
#: budget — and one build ran thirty-odd rounds on a single finding.
#: Six since gr688058: the cap is the backstop now, not the stop —
#: the loop ends early only on the failure the ask names (an unchanged
#: reviewed head, a stale-read verdict, the same finding twice), so a
#: converging build→review loop gets the rounds.
PRELAND_FIX_CAP = 6

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

#: The mechanical op that cuts a release of a second repo (td659522
#: step 3). Named, not imported: the job module registers separately.
REPO_RELEASE_OP = "repo_release"

#: The mechanical op that bumps a second repo's pin in precis after its
#: release node cut the tag (td659522 step 4). Named, not imported. Its
#: job hands a branch to ``land_slice`` and succeeds: the node closes
#: through that land, like a ``run_slice`` slice, never on the job itself.
PIN_BUMP_OP = "pin_bump"

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
#: (a live git's lock in the lander's own ship folder, gr684320; builders
#: no longer share a folder, so only a land stamps it),
#: ``"infra-land"`` (the gate's own machinery killed the land, gr570187),
#: ``"mcp-outage"`` (the launch's precis MCP preflight got no transport
#: answer — the server or its proxy was down, gr731751 closing gr707371).
ATTEMPT_FREE_KEY = "attempt_free"

#: ``run_slice.MCP_OUTAGE_KEY``, restated — the same one-way rule (a test
#: pins the two spellings equal). The launch's precis MCP preflight got
#: no transport answer (timed out or connection refused; gr731751 closing
#: gr707371 — both symptoms live there): infra, not the slice's diff. The
#: free-failure run (:func:`_free_run`) bounds the wait and backs it off
#: (gr662077) until the MCP answers; an HTTP answer (the 401 bearer
#: rejection) never carries this key and still burns the attempt.
MCP_OUTAGE_KEY = "mcp-outage"

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

#: The launch cannot prove diversity on a reachable host or retry rung,
#: or lacks prior-session evidence. The tick asks the coordinator without
#: spending the retry budget. Mirrors ``run_slice.MODEL_RULE_KEY``.
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
#: sees them — an infra wait that never clears (a lane that never
#: frees, a live git in the land's ship folder) needs its own bound.
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

#: How long a land may be refused for capacity, every land since the
#: first refusal, before the tick files an ask (:func:`_capacity_wait_done`).
#: Until then the refusals ride the free-failure backoff
#: (:func:`free_backoff_ready_at`) and never count toward
#: :data:`LAND_AGAIN_CAP`.
LAND_CAPACITY_WAIT_CAP = timedelta(minutes=45)


def free_backoff_ready_at(newest_created_at: datetime, run: int) -> datetime | None:
    """When a node's free-failure backoff ends: the newest failed job's
    mint plus the exponential wait for a ``run``-long identical run, or
    ``None`` while the run is under :data:`FREE_RUN_GRACE` (immediate
    re-mint). One home for the formula — the dispatch step applies it and
    the stall watchdog reads it to tell a backoff wait from a stall
    (gr681342)."""
    if run < FREE_RUN_GRACE:
        return None
    return newest_created_at + min(
        FREE_BACKOFF_MAX, FREE_BACKOFF_BASE * 2 ** (run - FREE_RUN_GRACE)
    )


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

#: ``land_slice.CONFLICT_KEY``, restated (a test pins the two spellings
#: equal): the task meta ``{head, ref, base_tip, files, at}`` a land red on
#: a rebase or merge conflict against the base leaves (gr741695, Reto's
#: ruling gr735593). The lander rewrites it on every such red and clears
#: it with ``meta.build_ref`` on a land; a start-over clears it here.
CONFLICT_KEY = "conflict"

#: ``meta.conflict_tries`` on a task node — the conflict path's own record
#: ``{n, job, sent_back, at}`` (the lander rewrites :data:`CONFLICT_KEY`
#: on every conflict, so the count cannot live in it): ``n`` the newest
#: conflict attempt's try number, ``job`` its job id, ``sent_back`` the
#: attempt job whose resolution review said FIX (a failed try). Cleared
#: by a start-over, by an answered ask (a fresh budget) and by the tick
#: that sees the land.
CONFLICT_TRIES_KEY = "conflict_tries"

#: Resolution attempts a land conflict gets before the coordinator ask:
#: tries 1 and 2 on the rung the task's build ran on, try
#: :data:`CONFLICT_STRONG_TRY` on the strongest rung of the ladder.
CONFLICT_MAX_TRIES = 3
CONFLICT_STRONG_TRY = 3

#: ``dispatch_score`` floor of the reviewer and land jobs of a task whose
#: conflict resolution is pending — above :data:`FINISH_FIRST_SCORE` (16)
#: and the release class (32), so the resolution lands before any other
#: land moves the base again.
CONFLICT_FIRST_SCORE = 64.0

#: How long a pending resolution holds the other lands of its repository
#: and base, counted from its push. Past it a stuck review no longer
#: stops landing: the hold lapses with a note and other lands flow.
CONFLICT_LAND_HOLD = timedelta(minutes=45)

#: ``land_slice.RELEASE_SYNC_CONFLICT_MARK``, restated — the dependency
#: stays one-way (a test pins the two spellings equal): the lander's red
#: reason when merging the moved release head into a release fix's branch
#: conflicts (gr738762).
RELEASE_SYNC_CONFLICT_MARK = "release sync conflict"

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
        "move detail into a child skill or trim; the cap is in the "
        "skill-size test (test_skill_size)",
    ),
    (
        "skill-vocab",
        re.compile(r"appears? in no skill", re.IGNORECASE),
        "mention the named registry value in the skill that owns it "
        "(the skill-vocabulary test, test_skill_vocabulary)",
    ),
    (
        "release-conflict",
        # gr738762: a release fix's land conflicting with the moved release
        # head — the lander's own merge (:data:`RELEASE_SYNC_CONFLICT_MARK`)
        # or ship's merge-tree die. The base is the node's release branch,
        # not main, and the fix is a merge: the branch is already pushed.
        # ``{target}`` fills from the node (:func:`_mech_red`); the paths
        # ride in the quoted decisive line.
        re.compile(
            rf"{RELEASE_SYNC_CONFLICT_MARK}: merging origin/"
            r"|release conflict — ask the coordinator"
        ),
        "merge origin/{target} into the branch (never origin/main, never a "
        "rebase of pushed commits) and resolve the conflicted paths below, "
        "keeping the release's side for anything this fix does not need to "
        "change; then hand off again",
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
)

#: The error block header from land_slice.poll, restated to keep imports
#: one-way. A test pins the spelling.
_LAND_GATE_ERROR_HEADER = "The gate's error lines:"
_LINT_ERROR_RE = re.compile(
    r"^\S+\.py:\d+:(?:\d+: [A-Z]{1,4}\d{3,4} | error:)",
    re.MULTILINE,
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
#: Punctuation a glued token may directly follow: sentence-final
#: ``.!?`` (gr573564), plus ``:`` and ``)`` — a streamed text block
#: glued to its narration leaves ``…just parse):review: LAND`` with no
#: separator at all (gr687501).
_TOKEN_BOUNDARY_PUNCT = frozenset(".!?:)")
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
        or ``meta.preland_defer_failed`` on a blocker still ``done``
        (gr663738): the push died, the work sits only in an attempt
        folder, and a dependant's cut of main would miss it exactly
        like the held land. The hold does NOT outlive a drop ruling: a
        blocker closed ``won't-do`` (or another non-``done`` terminal
        status) is the coordinator abandoning the work, so it releases
        its dependants like any dead blocker (review td676510) — or,
        the blocker being the review itself, on its targets (the
        reviewer closes ``done`` at LAND while the in-flight land has
        not recorded). The load-time :attr:`_Blocker.pending_land` is
        honored on the loaded path too: the load query resolves it in
        the store under the same ``done`` condition, so it sees a
        reviewer's held targets that sit outside the loaded subtrees —
        invisible to the in-memory :func:`_pending_targets` (gr659981
        review td660658)."""
        node = self.nodes.get(b.id)
        if node is None:
            return b.pending_land
        if (
            isinstance(node.meta.get(_templates.PENDING_LAND_KEY), dict)
            or (
                node.status == "done"
                and node.meta.get(_templates.PRELAND_DEFER_FAILED_KEY)
            )
            or b.pending_land
        ):
            return True
        return _is_reviewer(node) and bool(_pending_targets(self, node))

    def blocked(self, n: _Node) -> bool:
        """A live ``blocked-by`` target on ``n`` or any ancestor in the
        plan — live meaning open, or terminal with its land still held
        (:meth:`_land_held`): the dependant's clone must cut from a
        main carrying the build, so ``done`` alone never releases it
        (gr659981). The one exemption is ``n`` itself being the pre-land
        reviewer gating that land (:func:`_is_its_land_gate`): its LAND
        verdict is what mints the held ``land_slice``, so holding it
        would deadlock the chain. A coordinator ask on its build holds
        that review even when sequencing would otherwise release it."""
        if _preland_stop_open(self, n):
            return True
        if _is_reviewer(n) and _preland_review_held(self, n):
            return True
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


#: Terminal statuses that are always a *ruling* on the work (gr663909):
#: on the reopen paths below ``done`` is machine state — the pre-land
#: hold, the post-land close, an ended digest — but a ``won't-do`` /
#: ``auto-timeout`` set while a job ran or a review queued outranks the
#: run's and the verdict's writeback: the node is never reopened.
_RULING_STATUSES: frozenset[str] = TERMINAL_TODO_STATUSES - {"done"}


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
                     -- gr663738: a failed pre-land defer holds the
                     -- blocker's dependants like pending_land does —
                     -- while it stays 'done'; a won't-do ruling drops
                     -- the hold (review td676510).
                     OR (b.meta->>'preland_defer_failed' IS NOT NULL
                         AND EXISTS (
                           SELECT 1 FROM ref_tags rt2 JOIN tags t2 USING (tag_id)
                            WHERE rt2.ref_id = b.ref_id
                              AND t2.namespace = 'STATUS' AND t2.value = 'done'))
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
    #: Queued candidates currently waiting on file overlap at claim.
    held: int = 0
    #: Why the pass did nothing (another tick held the lock); ``None`` = ran.
    skipped: str | None = None
    notes: list[str] = field(default_factory=list)
    #: Notes the tick job also records as ``job_result`` lines — a
    #: ``release_cut`` refusal, once per change (gr651885).
    logged: list[str] = field(default_factory=list)

    #: The current dispatch impediment per node, before summary truncation.
    dispatch_holds: dict[int, str] = field(default_factory=dict, init=False, repr=False)

    def dispatch_hold(self, n: _Node, reason: str) -> None:
        self.dispatch_holds[n.id] = reason
        self.notes.append(f"td{n.id}: {reason}")

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
            "held": self.held,
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
            f"reopened={self.reopened} reviews={self.reviews} held={self.held}"
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
        #: Comments a dry pass would have written, ``(ref_id, text)`` —
        #: later steps of the same pass read them (gr738860).
        self.dry_comments: list[tuple[int, str]] = []

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
        self.comment_id(n.id, text)

    def comment_id(self, ref_id: int, text: str) -> None:
        """:meth:`comment` on a todo outside the loaded plan."""
        if self.dry_run:
            self.dry_comments.append((ref_id, text))
            return
        existing = list(self.store.chunks.list_chunks_for_ref(ref_id))
        ord_ = max((int(c.ord) for c in existing), default=0) + 1
        self.store.chunks.insert_chunks(
            ref_id,
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
            _close_chain_source(store, plan, w, res, n)
        else:
            res.gates_closed += 1


def _close_chain_source(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, gate: _Node
) -> None:
    """A ``sha-in-prod`` gate just turned done under a root carrying
    ``meta.source_todo``: the chain's source todo goes done too
    (:func:`precis.plans.todo_chain.close_source`). Never fails the pass
    or the gate; the gate is already done, so a failed close is left to
    :func:`precis.plans.todo_chain.sweep_landed_sources`."""
    spec = gate.meta.get("gate") or {}
    if w.dry_run or spec.get("kind") != "sha-in-prod":
        return
    root = next(
        (a for a in plan.ancestors(gate) if SOURCE_TODO_KEY in a.meta),
        None,
    )
    sha = (spec.get("args") or {}).get("sha")
    if root is None or not isinstance(sha, str):
        return
    try:
        if close_source(store, root.id, sha):
            res.notes.append(
                f"td{root.id}: source todo td{root.meta[SOURCE_TODO_KEY]} "
                f"closed ({sha[:9]} is in prod)"
            )
    except Exception as exc:
        log.warning(
            "plan_dispatch: closing the source of td%d failed", root.id, exc_info=True
        )
        res.notes.append(f"td{root.id}: source todo close FAILED ({exc})")


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
        if node_kind(n.meta) != "loop" or plan.paused(n):
            continue
        spec = dict(n.meta.get("loop") or {})
        body = spec.get("body")
        if _closed(n) and not (
            n.status == "done" and body == "release-fix" and spec.get("exhausted_ask")
        ):
            continue
        if plan.blocked(n):
            continue
        # A parked loop or an unfinished iteration still owns the work.
        if any(t.startswith("waiting-for:") for t in n.open_tags):
            continue
        if not all(_closed(c) or _informational_ask(c) for c in plan.child_todos(n)):
            continue
        try:
            count = int(spec.get("count") or 0)
            cap = None if body == "release-fix" else int(spec.get("max") or 0)
        except (TypeError, ValueError):
            _close_loop(w, res, n, f"meta.loop count/max unreadable: {spec!r}")
            continue
        until = _gates.evaluate(store, spec.get("until") or {}, todo_id=n.id, now=now)
        if until.open:
            if not _closed(n):
                _close_loop(w, res, n, f"until is met ({until.why})")
            continue
        if cap is not None and count >= cap:
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
        try:
            with nullcontext() if w.dry_run else store.atomic() as conn:
                if body == "release-fix":
                    # Serialize with deploy launch and any moved-head poll.
                    if conn is not None:
                        conn.execute(_release._LOCK_ROOT_SQL, (n.root,))
                        ref = store.get_ref(kind="todo", id=n.id, conn=conn)
                        if ref is None or ref.meta.get("loop") != spec:
                            continue
                    release = _release.release_of(store, n.id)
                    if (
                        release is None
                        or _release.deploy_started(store, n.id) is not None
                        or _release.newest_red_run(store, str(release.get("sha") or ""))
                        is None
                    ):
                        continue
                if not w.dry_run:
                    _templates.mint_body(store, n.id, body, iteration=count + 1)
                next_spec = {**spec, "count": count + 1}
                next_spec.pop("exhausted_ask", None)
                w.stamp_meta(n, {"loop": next_spec})
                if _closed(n):
                    w.set_status(n, "open")
        except BadInput as exc:
            _close_loop(w, res, n, f"loop body {body!r} refused: {exc.cause}")
            continue
        res.loops_minted += 1
        suffix = f"/{cap}" if cap is not None else ""
        res.notes.append(f"loop td{n.id}: minted {body} iteration {count + 1}{suffix}")


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
    boundary punctuation — sentence-final ``.!?`` (``…main.review: FIX
    — …``), plus the ``:`` and ``)`` a streamed text block glued to its
    narration leaves before the token (``…just parse):review: LAND``,
    gr687501); a token
    mid-sentence (``I will post review: LAND if…``) is narration. Markdown
    line-start markers — ``- ``/``* `` bullets, ``**`` bold, ``#``
    headings, ``> `` quotes — may sit between the line start and the
    token."""
    i = start - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0 or text[i] == "\n" or text[i] in _TOKEN_BOUNDARY_PUNCT:
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
    """The sha of the todo's newest ``landed: <sha>`` comment; a newer
    ``landed: nothing — …`` is skipped. ``run_slice._newest_landed_sha``
    reads the same, so a second-repo reviewer reviews this commit."""
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
    extra_meta: dict[str, Any] | None = None,
) -> int:
    """Insert a coordinator ask and expose it to later steps this pass.

    A task configuration ask also links the node ``blocked-by`` the ask.
    The dispatch tick's lock and that edge dedupe subsequent passes.
    FIX-chain asks retain the root-wide coordinator tag as their hold.
    ``extra_meta`` adds top-level keys beside ``meta.ask`` (never inside
    it — ``check_ask_meta`` refuses unknown ask keys): the marker a
    later pass seeks the ask by (:func:`_park_preland_rounds`, gr738860).
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
    if extra_meta:
        meta.update(extra_meta)
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


def _release_scope_target(
    store: Any, shas: list[str], todos: Sequence[int] = ()
) -> str | None:
    """``release/r<N>`` when one of ``shas`` is inside the open release's
    scope — its cut range or a commit on the release branch
    (``release_poll._release_lands``, the deploy gate's own scope) —
    and that release still takes fixes (no deploy started); else
    ``None`` (the corrective lands on main). The deploy gate wants a
    FIX/REVERT's corrective ON the release branch (release-train T4),
    so a corrective minted for main would hold the deploy until a human
    answers. A main commit the branch repeats patch for patch
    (``release_poll._branch_carries``) is in the scope too: the gate
    judges the branch's copy by that commit's review, so its corrective
    belongs on the branch as well.

    ``todos`` — the reviewed todos, passed for a FIX only — adds the
    gate's other link (gr741488): an engine commit only the branch has
    whose subject's trailing handle list names one of them
    (``release_poll._land_tds``) is that todo's unit at the gate even
    when it is no patch-for-patch copy, a hand carry with a fix-up
    folded in. Its FIX holds the deploy on the corrective, so the
    corrective goes to the branch. A REVERT keeps the sha rule: it
    reverts the named commit, which that branch copy is not. Any read
    fault reads ``None``."""
    if not shas and not todos:
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
        picked, undone, _notes = _rp._branch_carries(rel, lands)
    except Exception:
        log.warning("plan_dispatch: release scope unreadable", exc_info=True)
        return None
    branch = _release.release_branch(str(rel["rN"]))
    for sha in [land.sha for land in lands] + [src.sha for src in picked.values()]:
        if any(sha.startswith(s.lower()) or s.lower().startswith(sha) for s in shas):
            return branch
    wanted = set(todos)
    for land in lands:
        if not wanted or not land.branch_only or land.sha in undone:
            continue
        if _lanes.engine_hits(land.paths) and wanted.intersection(
            _rp._land_tds(land.subject)
        ):
            return branch
    return None


def _mint_fix_reviewer(
    store: Any,
    plan: _Plan,
    w: _Writer,
    n: _Node,
    fix_id: int,
    repo: str,
    now: datetime,
) -> None:
    """Mint the reviewer of the second-repo fix ``fix_id`` in the same pass
    (td659522 step 5): ``mint_reviewer`` gives it the repo tag and the
    ``blocked-by`` edge on the fix; the in-memory plan gets the node (like
    :func:`_mint_task`, so later steps of this pass see it) and the open
    release node waits on it too. A fix of a second repo would otherwise
    land unreviewed; the FIX depth cap bounds the chain. The caller skips a
    dry run (nothing is minted)."""
    rid = _templates.mint_reviewer(store, n.root, fix_id, repo=repo)
    ref = store.get_ref(kind="todo", id=rid)
    root = plan.nodes.get(n.root)
    plan.nodes[rid] = _Node(
        id=rid,
        kind="todo",
        parent_id=n.root,
        title=str(ref.title or "") if ref is not None else "",
        meta=dict(ref.meta or {}) if ref is not None else {},
        status="open",
        created_at=now,
        prio=None,
        depth=(root.depth + 1) if root is not None else 1,
        root=n.root,
        open_tags={f"plan:{n.root}": now, f"repo:{repo}": now},
    )
    if root is not None:
        root.children.append(rid)
    plan.blockers.setdefault(rid, []).append(
        _Blocker(id=fix_id, status="open", is_ask=False, created_at=now)
    )
    _gate_release_on(store, plan, w, n, rid, now)


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
    repo = _second_repo(plan, n)
    # A second repo's sha is not a precis sha: its release scope is never
    # read (td659522 step 5).
    target = (
        None
        if repo is not None
        else _release_scope_target(
            store,
            _reviewed_shas(store, plan, n),
            [t.id for t in _review_targets(plan, n)],
        )
    )
    if repo is not None:
        body = (
            f"{findings}\n\nFindings of the review td{n.id} of {handle}'s land on "
            f"{repo} {_repo_base(repo)}. Fix them on a fresh slice in the {repo} "
            f"repo; the factory's lander lands it; {intent}."
        )
    elif target is None:
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
    _tag_minted_repo(plan, w, n, fix_id, now)
    _gate_release_on(store, plan, w, n, fix_id, now)
    if repo is not None and not w.dry_run:
        _mint_fix_reviewer(store, plan, w, n, fix_id, repo, now)
    w.stamp_meta(node, {FIX_DEPTH_KEY: depth})
    if target is not None:
        w.stamp_meta(node, {_release.LAND_TARGET_KEY: target})
    if depth >= FIX_ESCALATE_DEPTH:
        # gr604629: two rounds could not close the finding — the third is
        # born model-pinned to the ladder's top rung (the ``model`` pin
        # routes through ``pick_lane`` and ``_escalate`` never climbs it
        # back down), not held for another design call. The top is
        # ``top_rung`` -- the table's last lane's last model -- not
        # ``ladder()[-1]``, whose round-robin order ends on whichever lane
        # lists the most models (gr685753).
        task_class = str(node.meta.get("task_class") or _DEFAULT_TASK_CLASS)
        top = _lanes.top_rung(task_class, lanes=lane_table)
        if top is not None:
            params = _lanes.pin_params(top)
            w.stamp_meta(node, {"op": {"type": DEFAULT_OP, "params": params}})
        pin = _lanes.pin_label(top)
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
    # reverted on its release branch, where the deploy gate wants it. A
    # second repo's sha is no precis sha: its scope is never read.
    repo = _second_repo(plan, n)
    target = None if repo is not None else _release_scope_target(store, shas)
    where = target or (f"{repo} {_repo_base(repo)}" if repo else "main")
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
    if repo is not None:
        cmds = (
            ", ".join(f"`git revert --no-edit {s}`" for s in shas)
            if shas
            else "`git revert --no-edit <sha>` for each commit naming " + handle
        )
        body = (
            f"Revert {named} in the {repo} repo: a builder slice whose whole "
            f"change is {cmds} (newest first); no other edit, no version "
            "bump, no push. The factory's lander lands it."
            f"\n\nWhy (review td{n.id}): {why}"
        )
    elif target is None:
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
    _tag_minted_repo(plan, w, n, revert_id, now)
    _gate_release_on(store, plan, w, n, revert_id, now)
    if target is not None:
        w.stamp_meta(plan.nodes[revert_id], {_release.LAND_TARGET_KEY: target})
    requeue = [t for t, sha in landed if sha] or targets
    for t in requeue:
        if t.status in _RULING_STATUSES:
            # gr663909: a coordinator ruling on the landed node outranks
            # the REVERT requeue — the verdict keeps its minted revert
            # task but never reopens the ruled node behind it.
            w.comment(
                t,
                f"plan_dispatch: review REVERT (td{n.id}) — td{t.id} is "
                f"STATUS:{t.status}, a ruling set while the review ran; "
                "it stands and the requeue reopens nothing",
            )
            continue
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


def _defer_failed(store: Any, todo_id: int) -> bool:
    """``meta.preland_defer_failed`` on ``todo_id`` (gr663738): the
    build's last pre-land defer raised — its work never reached the
    hub, it sits only in an attempt folder — so a ``done`` with
    no ``landed:`` is a lost push, not a dead land: neither the verdict
    close nor the dead-chain sweep may drop it silently."""
    ref = store.get_ref(kind="todo", id=todo_id)
    return bool(
        ref is not None and (ref.meta or {}).get(_templates.PRELAND_DEFER_FAILED_KEY)
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


def _open_preland_reviewers(plan: _Plan, n: _Node) -> list[_Node]:
    """The open pre-land reviewers whose land gate covers ``n`` (gr681208):
    a build a FIX/REVERT sent back keeps its reviewer open to re-gate the
    rebuild — a ``validated:``/``no-op:`` on it is "the pushed branch is
    ready as it stands", never proof nothing is left to land."""
    return [
        m
        for m in plan.todos()
        if not _closed(m)
        and _is_reviewer(m)
        and m.meta.get(_templates.PRELAND_REVIEW_KEY) is True
        and (
            _review_from(m) == n.id
            or any(t.id == n.id for t in _review_targets(plan, m))
        )
    ]


def _dead_land_targets(plan: _Plan, store: Any, n: _Node) -> list[int]:
    """The reviewer ``n``'s land targets when EVERY one closed with no
    ``landed:`` comment — the review has nothing left to judge
    (gr575912). ``[]`` while any target is still live or landed one: a
    ``won't-do`` blocker is terminal, so without this check a build the
    coordinator killed frees its review for dispatch anyway.

    Targets are ``generate.from``'s todo — its non-ask children when it
    has them (a slicer's slices) — falling back to the non-ask
    ``blocked-by`` edges when ``from`` resolves to no plan todo (an
    engine-review node points out of its plan subtree). A target stamped
    ``meta.preland_defer_failed`` still counts as dead here — the read
    IS right that nothing landed; :func:`_close_dead_chain` turns that
    case into an ask instead of a close while the target still claims
    ``done``, and honors a ``won't-do`` drop ruling as a plain close
    (gr663738, review td676510).

    A sha-keyed reviewer (:func:`_review_sha`) with no td target has
    nothing that could fail to land, so it is never dead here whatever
    its blockers did."""
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
        elif _review_sha(n) is not None:
            # A sha-keyed reviewer judges a commit already on main: it has
            # no land that could fail. Its ``blocked-by`` edges are other
            # work (a rescue's diagnose task), whose close without a
            # ``landed:`` comment is not "nothing landed to review"
            # (td704507, td728642).
            return []
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
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    dead: list[int],
    now: datetime,
) -> None:
    """Close reviewer ``n`` and the rest of the dead land chain ``won't-do``
    (gr575912): the dead target's other ``blocked-by`` descendants — the
    prod gate, the validate — starve with the review, so the pass runs
    the same close a task moot runs (``_step_moot``) and the root can
    settle. A node carrying a live job defers: the sweep re-runs every
    pass while the targets stay dead (``_step_reviews`` runs it for a
    closed reviewer too), so the straggler closes once its job ends
    instead of holding the root open forever.

    The carve-out (gr663738): a dead target stamped
    ``meta.preland_defer_failed`` is a lost push, not a dead land — its
    reviewed, amended commit sits only in an attempt folder. Closing the
    chain won't-do would drop that work and the never-built dependants
    with no trace, so the sweep files ONE coordinator ask and closes
    nothing; the ask's ``blocked-by`` edge on ``n`` parks the review and
    dedupes later passes, and the stamp holds the dependants
    (:meth:`_Plan._land_held`).

    Resolution (review td676510): the stamp only counts while the
    target still claims ``done`` — a stamped target the coordinator
    closed ``won't-do`` is the explicit drop ruling the ask offers, so
    it falls back to the normal close below and the chain settles
    instead of re-filing the ask forever."""
    stamped = [
        tid
        for tid in dead
        if _defer_failed(w.store, tid) and _todo_status(w.store, tid) == "done"
    ]
    if stamped:
        names = ", ".join(f"td{t}" for t in stamped)
        if plan.open_asks(n):
            res.notes.append(
                f"td{n.id}: dead land chain holds a failed pre-land defer "
                f"({names}) — its ask already stands"
            )
            return
        if w.dry_run:
            res.asks += 1
            res.notes.append(
                f"td{n.id}: would ask — dead land chain holds a failed "
                f"pre-land defer ({names})"
            )
            return
        ask_id = _mint_coordinator_ask(
            w.store,
            plan,
            n,
            title=(
                f"td{n.id}: dead land chain holds a failed pre-land defer "
                "— re-push the branch or drop the chain?"
            ),
            body=(
                f"The plan tick found review td{n.id} nothing left to "
                f"judge: {'its land target' if len(dead) == 1 else 'all of its land targets'} "
                f"({', '.join(f'td{t}' for t in dead)}) closed `done` "
                "with no `landed:` comment — the usual dead-chain close "
                "would drop the review and every starved dependant "
                "won't-do. But "
                f"{names} carries `meta.preland_defer_failed`: its "
                "build's work never reached its host's hub, so the "
                "commit exists only in an attempt folder "
                "(`slice-td<id>-j<job>` on the host the newest build "
                "job's `meta.push` names) — closing the chain drops "
                "real, reviewed work (gr663738). The tick closed "
                "nothing and parked this review on this ask. To keep "
                "the work: take the sha the stamp names from that "
                "folder (or re-run the build), push it to that "
                "host's hub as `refs/factory/build/<id>/<job>`, "
                "clear the target's "
                "`meta.preland_defer_failed` and reopen it — the "
                "reviewer re-gates on the next defer. To drop it: close "
                f"{'the target' if len(stamped) == 1 else 'each stamped target'} "
                "STATUS:won't-do and the sweep closes the rest. "
                "Answering 'acknowledged' and closing this ask leaves "
                "the chain parked as-is; the sweep re-files next tick."
            ),
            context=(
                f"review td{n.id}: dead land target(s) {names} carry "
                "meta.preland_defer_failed — the work never reached "
                "the hub"
            ),
            now=now,
            block_node=True,
        )
        res.asks += 1
        res.notes.append(
            f"td{n.id}: dead land chain holds a failed pre-land defer "
            f"({names}) — nothing closed, asked in td{ask_id}"
        )
        return
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
    store: Any, plan: _Plan, w: _Writer, res: PassResult, n: _Node, t: _Node
) -> int:
    """The pre-land LAND (gr570506): mint the ``land_slice`` the build's
    clean exit held back — the params ``run_slice`` stamped in
    ``meta.pending_land``, the usual ``land_slice:td<id>`` idem key.
    ``meta.pending_land`` STAYS stamped until ``land_slice`` itself lands
    (``_record_land`` clears it): it is the marker keeping the done,
    not-yet-landed target out of ``_dead_land_targets`` while the land
    is in flight, and the stamp ``land_slice`` reads to admit a closed
    todo. The caller gates this on the verdict's judged head: a LAND
    naming a head other than the stamp's must not mint — it read a
    different commit than the held land ships (td730820). The land is
    stamped with the build's :func:`_claim_score`, floored so it claims
    ahead of fresh builds. Returns the job id."""
    params = dict(t.meta[_templates.PENDING_LAND_KEY])
    # The land runs where the hub holds the build (gr741695): the stamp's
    # own ``target_node`` is the same host; the task's record wins when
    # they differ, and a stamp without one gets it from the record.
    pin = _hub_pin(plan, store, t, "land_slice")
    meta_extra: dict[str, Any] = {
        _dscore.SCORE_META_KEY: _claim_score(plan, t, job_type="land_slice")
    }
    if pin is not None:
        override = _pin_job_host(params, pin[0], pin[1])
        meta_extra[HUB_PIN_KEY] = {"host": pin[0], "todo": pin[1]}
        if override is not None:
            res.notes.append(f"td{t.id}: {override}")
    job_id = _mint_job(
        store,
        t,
        "land_slice",
        "ssh_node",
        params,
        idem_key=f"land_slice:td{t.id}",
        meta_extra=meta_extra,
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


def _same_sha(a: str, b: str) -> bool:
    """One sha a prefix of the other — a 40-char pushed tip against the
    abbreviation a verdict writes (gr688058)."""
    a, b = a.lower(), b.lower()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


#: The ``path/file.py::Symbol`` citation a finding carries (gr688058):
#: a shared active anchor needs similar finding text to repeat the
#: finding again — the builder's response is not reaching the review.
_ANCHOR_RE = re.compile(r"[\w./~-]+::[\w.~-]+")

#: Where one clause ends and the next begins — ``;``, a sentence end,
#: an em dash, a newline or a comma join (td688394). Bounds the
#: judged-head scan: a sha is judged only inside its own clause.
_FINDING_CLAUSE_RE = re.compile(r"[;!?\n—]+|\.\s+|,\s*(?:and|but|so|yet)?\s*")

#: The ``anchors:`` line the pre-land reviewer brief asks a FIX/REVERT
#: verdict to end with — the ``file::function`` of each ACTIVE finding
#: it sends back (gr688058, td688394 r5). The tick reads the LAST such
#: line and never classifies the prose: a resolved-context cite
#: (``a.py::f is fixed``) and a fresh finding read the same to a
#: regex, so the reviewer declares the active set on this line. No
#: ``anchors:`` line, no anchors.
_ANCHORS_LINE_RE = re.compile(
    r"^[ \t]*anchors\s*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE
)


def _finding_anchors(rest: str) -> set[str]:
    """The ``file::function`` anchors ``rest``'s last ``anchors:`` line
    names, lowercased — the active findings the reviewer declares
    (gr688058, td688394 r5); empty when the line is absent or bare.
    Prose is never parsed for them."""
    line = ""
    for m in _ANCHORS_LINE_RE.finditer(rest):
        line = m.group(1)
    return {a.group(0).lower() for a in _ANCHOR_RE.finditer(line)}


#: A sha token: a 7-to-40-hex word carrying at least one digit, so a
#: ``td<id>`` cite or a word like ``defaced`` never reads as one.
_SHA_TOKEN = r"\b(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b"

#: The sha a verdict names as the head it JUDGED — ``verified head
#: <sha>`` / ``reviewed <sha>`` / ``rev-parse <sha>`` / ``<sha>
#: verified`` on one line (gr688058). Naming no head proves nothing; a
#: named head other than the pushed tip proves the read was stale.
_JUDGED_HEAD_RES = (
    re.compile(
        rf"\b(?:verified|reviewed|judged|rev-parse|tested)\b[^\n]{{0,40}}?`?"
        rf"({_SHA_TOKEN})",
        re.IGNORECASE,
    ),
    re.compile(rf"({_SHA_TOKEN})[^\n]{{0,30}}?\bverified\b", re.IGNORECASE),
)

#: ``head <sha>`` / ``tip <sha>`` — a judged head only when no judgment
#: verb named one; under an expectation gloss it names what the branch
#: should be at, never what the reviewer read (td688394).
_HEAD_TIP_RE = re.compile(
    rf"\b(?:head|tip)\b[^\n]{{0,40}}?`?({_SHA_TOKEN})", re.IGNORECASE
)

#: ``expected head <sha>`` / ``pushed tip <sha>`` — an expectation
#: gloss names what the branch should be at, never the head the
#: reviewer read. The whole mention is masked out of its clause before
#: the judgment markers scan it, so its sha can never be judged —
#: ``expected head <current>; verified head <old>`` judges ``<old>``
#: however the clauses are joined (td688394 r2).
_EXPECT_SHA_RE = re.compile(
    rf"\b(?:expected|pushed|current|new|actual|want(?:ed)?)\b"
    rf"[^\n]{{0,24}}?(?:head|tip)\b[^\n]{{0,16}}?`?{_SHA_TOKEN}",
    re.IGNORECASE,
)


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with every ``(start, end)`` span blanked to spaces —
    positions unchanged, so a match's offset still orders the text."""
    chars = list(text)
    for s, e in spans:
        chars[s:e] = " " * (e - s)
    return "".join(chars)


def _verdict_heads(raw: str) -> list[str]:
    """The shas ``raw`` names as the head it JUDGED, in text order —
    what the reviewer actually read (gr688058). A judgment verb's sha
    (``verified head <sha>``, ``<sha> verified``) is that head — read
    inside the verb's own clause (the finding-clause boundaries), so a
    neighbouring clause's sha is never judged, and with every
    expectation mention (``expected head <sha>``, ``pushed tip
    <sha>``) masked first, so a sha the verdict itself marks as
    expected can never be judged either (td688394 r2); a bare
    ``head``/``tip`` mention counts only when no verb named one.
    ``"verified head <old>; expected head <current>"`` reads as the
    stale judged head it is (td688394)."""
    clauses: list[tuple[int, str]] = []
    start = 0
    for m in _FINDING_CLAUSE_RE.finditer(raw):
        clauses.append((start, raw[start : m.start()]))
        start = m.end()
    clauses.append((start, raw[start:]))
    masked = [
        (base, _mask_spans(clause, [m.span() for m in _EXPECT_SHA_RE.finditer(clause)]))
        for base, clause in clauses
    ]
    seen: dict[str, int] = {}
    for base, clause in masked:
        for rx in _JUDGED_HEAD_RES:
            for m in rx.finditer(clause):
                seen.setdefault(m.group(1).lower(), base + m.start(1))
    if seen:
        return sorted(seen, key=seen.__getitem__)
    for base, clause in masked:
        for m in _HEAD_TIP_RE.finditer(clause):
            seen.setdefault(m.group(1).lower(), base + m.start(1))
    return sorted(seen, key=seen.__getitem__)


#: Token-Jaccard floor of the newest finding's text against the last
#: round's for a shared ``anchors:`` entry to count as the same finding
#: (gr702711): a new finding at the same function reads differently.
PRELAND_SAME_FINDING_JACCARD = 0.6

#: Longest finding text kept in the ``preland_fix_rounds`` record.
_FINDING_TEXT_CAP = 4000

_SHA_RE = re.compile(_SHA_TOKEN)


def _finding_text(kind: str, rest: str) -> str:
    """``rest`` as the loop compares findings: the ``anchors:`` line and
    sha tokens dropped (a fresh verified head is not a new finding),
    lowercased, whitespace collapsed."""
    body = _ANCHORS_LINE_RE.sub("", rest.lower())
    body = _SHA_RE.sub("", body)
    return " ".join(f"{kind} {body}".lower().split())


def _token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity of the word-token sets of ``a`` and ``b``."""
    ta, tb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


#: Why the pre-land review loop stops (:func:`_preland_round`), each with
#: the reading its coordinator ask gives — the four stops need different
#: answers, and one shared sentence made the asks read alike (gr709688).
_PRELAND_STOPS: dict[str, str] = {
    "same finding": (
        "the rebuild did not resolve the finding, or the reviewer is wrong "
        "about it; the verdict names no head other than the pushed tip"
    ),
    "denied path": (
        "the rebuild returned no-op and the review requires a change "
        "outside the builder lane's allowed paths"
    ),
    "unchanged head": (
        "the rebuild pushed no new commit, so the reviewer judged the same "
        "tree again: read the rebuild job for why it committed nothing (a "
        "rebase conflict, a no-op verdict)"
    ),
    "stale read": (
        "the reviewer judged an older head than the one pushed, so its "
        "finding may already be fixed; nothing is known to be wrong with "
        "the pushed tip until a review reads it"
    ),
    "round cap": (
        "every round found something new on a new head; the build is not "
        "converging — its scope may be too large for one slice"
    ),
}


def _preland_round(
    n: _Node,
    t: _Node,
    kind: str,
    rest: str,
    pushed: str,
    raw: str,
    single: bool,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any], tuple[str, str] | None]:
    """``(count, record, stop)`` of the pre-land FIX/REVERT sent
    back to ``t`` (gr684815, gr688058): ``count`` one more than the
    ``meta.preland_fix_rounds`` stamp holds, ``record`` the value to
    write while the loop goes on (the finding's digest and anchors plus
    the pushed head this verdict judged; since gr762396 also the
    send-back time ``at``, the ``reviewer`` that wrote it and the
    verdict as written, ``kind`` and ``verdict``), and ``stop`` — the
    ``(reason, why)`` for :func:`_park_preland_rounds`, ``reason`` a
    :data:`_PRELAND_STOPS` key — ``None`` while the loop should run.

    The stop is the failure the ask describes, never the round count
    alone: the newest finding is the last one's again (same digest of the
    text with anchors line and shas dropped, or a shared
    ``file::function`` on the ``anchors:`` line together with token
    Jaccard >= :data:`PRELAND_SAME_FINDING_JACCARD`; a shared anchor
    alone is a new finding at the same function, gr702711),
    the reviewed head is unchanged
    since the previous send-back (the builder is not acting; a continuing
    attempt is rebased onto the base at the cut, so this stop needs the
    base not to have moved between the pushes), the head
    the verdict JUDGED is not the pushed tip (the reviewer is reading a
    stale branch — read only on a single-target review, whose verdict
    can name just the one tip), or the count reaches
    :data:`PRELAND_FIX_CAP`. A review a coordinator ``brief:`` bought
    on the rejected head (``prev["exempt"] == "brief"``, gr762396) never
    stops on the unchanged head: a new finding there is an ordinary
    send-back, and only the same finding again stops. A record another
    review node wrote (``reviewer``) is not this one's: the verdict
    starts a fresh record at round 1 (a record with no ``reviewer``
    counts as this node's)."""
    prev = t.meta.get(_templates.PRELAND_FIX_ROUNDS_KEY)
    prev = prev if isinstance(prev, dict) else {}
    wrote = prev.get("reviewer")
    if isinstance(wrote, int) and wrote != n.id:
        # gr762396: another review node wrote the kept record (the gate
        # was re-minted) — this verdict starts its own record at round
        # 1, and that record guards the loop from here.
        prev = {}
    try:
        count = int(prev.get("count") or 0) + 1
    except (TypeError, ValueError):
        count = 1
    text = _finding_text(kind, rest)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    anchors = sorted(_finding_anchors(rest))
    record: dict[str, Any] = {
        "count": count,
        "finding": digest,
        "head": pushed or None,
        "anchors": anchors,
        "text": text[:_FINDING_TEXT_CAP],
        "at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "reviewer": n.id,
        "kind": kind,
        "verdict": rest[:_FINDING_TEXT_CAP],
    }
    # The stale read is judged first: a reviewer that read the old head
    # re-reports the old finding, which would otherwise read as "same
    # finding" and blame the builder (td707034).
    heads = _verdict_heads(raw) if single else []
    if heads and pushed and not any(_same_sha(h, pushed) for h in heads):
        return (
            count,
            record,
            (
                "stale read",
                f"review td{n.id} judged head {heads[0][:12]} but the pushed "
                f"tip is {pushed[:12]} — the reviewer read a stale branch",
            ),
        )
    prev_anchors = set(prev.get("anchors") or [])
    same_digest = digest == prev.get("finding")
    if same_digest or (
        anchors
        and set(anchors) & prev_anchors
        and _token_jaccard(text, str(prev.get("text") or ""))
        >= PRELAND_SAME_FINDING_JACCARD
    ):
        return (
            count,
            record,
            (
                "same finding",
                f"review td{n.id} returned the same pre-land {kind} twice "
                "running "
                + (
                    "(same normalized finding digest)"
                    if same_digest
                    else "(shared active anchor and token Jaccard similarity "
                    f">= {PRELAND_SAME_FINDING_JACCARD})"
                ),
            ),
        )
    prev_head = str(prev.get("head") or "")
    if (
        pushed
        and prev_head
        and _same_sha(pushed, prev_head)
        and prev.get("exempt") != "brief"
    ):
        return (
            count,
            record,
            (
                "unchanged head",
                f"review td{n.id} sent the branch back on an unchanged head "
                f"{pushed[:12]} — the rebuild pushed nothing new",
            ),
        )
    if count >= PRELAND_FIX_CAP:
        return (
            count,
            record,
            (
                "round cap",
                f"review td{n.id} sent the branch back {count} times in a row",
            ),
        )
    return count, record, None


#: Top-level ask meta key marking an ask :func:`_park_preland_rounds`
#: minted: ``{node, reason, head, repeats}`` (gr738860). Top level, not
#: inside ``meta.ask`` — ``check_ask_meta`` refuses unknown ask keys.
PRELAND_STOP_KEY = "preland_stop"


def _open_preland_stop_ask(plan: _Plan, t: _Node) -> _Node | None:
    """The OPEN ask :func:`_park_preland_rounds` already minted for the
    build ``t`` (gr738860), lowest id first; ``None`` when there is none.
    A closed (answered) ask never counts: its answer re-armed the slice
    and the next stop is a new question."""
    for m in sorted(plan.nodes.values(), key=lambda m: m.id):
        stop = m.meta.get(PRELAND_STOP_KEY) if m.kind == "todo" else None
        if m.kind != "todo" or _closed(m):
            continue
        if isinstance(stop, dict) and stop.get("node") == t.id:
            return m
        # Stops minted before the marker used this same title prefix.
        if _is_ask_node(m) and m.title.startswith(
            f"td{t.id}: pre-land review loop stopped ("
        ):
            return m
    return None


def _preland_stop_open(plan: _Plan, t: _Node) -> bool:
    """Read the stop marker even if the ask's blocker edge is missing."""
    return (
        any(
            isinstance(t.meta.get(key), dict)
            for key in (
                _templates.PENDING_LAND_KEY,
                _templates.PRELAND_HELD_KEY,
                _templates.PRELAND_FIX_ROUNDS_KEY,
            )
        )
        and _open_preland_stop_ask(plan, t) is not None
    )


def _preland_review_held(plan: _Plan, n: _Node) -> bool:
    """A held build's coordinator ask also holds its read-only review."""
    return any(
        _preland_stop_open(plan, t)
        or (
            (
                n.meta.get(_templates.PRELAND_REVIEW_KEY)
                or isinstance(t.meta.get(_templates.PENDING_LAND_KEY), dict)
                or isinstance(t.meta.get(_templates.PRELAND_HELD_KEY), dict)
            )
            and plan.open_asks(t)
        )
        for t in _review_targets(plan, n)
        if t.status not in _RULING_STATUSES
    )


# Match affirmative duplicate findings, independently of the judged-head SHA.
_ALREADY_ON_MAIN = re.compile(
    r"(?:^|[.!?]\s+|\n)\s*(?:(?:the |this )?"
    r"(?:fix|change|work|patch)\s+(?:is|was)\s+)?"
    r"already (?:landed )?on (?:`?origin/`?)?`?main`?\b",
    re.IGNORECASE,
)


def _park_preland_rounds(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    t: _Node,
    kind: str,
    rest: str,
    stop: tuple[str, str],
    now: datetime,
    pushed: str = "",
    record: dict[str, Any] | None = None,
) -> None:
    """Hold the sent-back build ``t`` on a coordinator ask instead of
    rebuilding again (gr684815, gr688058): ``stop`` is
    :func:`_preland_round`'s ``(reason, why)`` — the same finding twice
    running, an unchanged reviewed head, a stale-read verdict, or
    :data:`PRELAND_FIX_CAP` rounds — or a no-op rebuild on a denied
    finding. The ask's title names the reason and
    its body that reason's own reading (:data:`_PRELAND_STOPS`).

    Only the count resets (gr762396): ``record`` — the round record of
    the verdict that stopped, else the standing stamp — is kept with
    ``count`` 0, so its head, finding digest, anchors and text survive
    the answer. The first round after it still stops on an unchanged
    head or the same finding, and :func:`_step_reviews` never releases
    the reviewer onto the head it rejected. A stale read keeps no head:
    the reviewer never judged the pushed tip, so a re-review of it is
    the answer, not a loop. ``stopped_at`` dates the stop: a ``brief:``
    on the reviewer or a builder ``validated:`` newer than it buys the
    head one more review (:func:`_rehold_exemption`); a spent exemption
    mark (``exempt``) does not survive the park.

    One open ask per slice (gr738860): the ask carries
    ``meta.preland_stop = {node, reason, head, repeats}``, and a stop on
    a build that already has such an ask open mints nothing — the slice
    is held either way and one answer re-arms it. The existing ask gets
    one comment (reason, why, head, reviewer), its ``repeats`` bumped and
    its reason/head updated, and ``t`` stays ``blocked-by`` it.
    ``pushed`` is the pushed head the verdict judged (``""`` for none)."""
    reason, why = stop
    head = pushed or None
    if record is None:
        record = _dict_of(t.meta.get(_templates.PRELAND_FIX_ROUNDS_KEY))
    kept: dict[str, Any] = {
        **{k: v for k, v in record.items() if k != "exempt"},
        "count": 0,
        "stopped_at": now.astimezone(UTC).isoformat(),
    }
    if reason == "stale read":
        kept["head"] = None
    w.stamp_meta(t, {_templates.PRELAND_FIX_ROUNDS_KEY: kept})
    existing = _open_preland_stop_ask(plan, t)
    if existing is not None:
        prior = existing.meta.get(PRELAND_STOP_KEY)
        try:
            repeats = int(prior.get("repeats") or 0) + 1 if prior else 1
        except (TypeError, ValueError):
            repeats = 1
        if w.dry_run:
            res.notes.append(
                f"td{t.id}: would note the repeat stop on ask td{existing.id} — {why}"
            )
            return
        w.comment_id(
            existing.id,
            f"The pre-land review loop stopped again on td{t.id} ({reason}): "
            f"{why}. Head: {head or 'none'}. Reviewer: td{n.id}. "
            f"Repeat {repeats} on this ask — no new ask minted; one answer "
            "re-arms the slice (gr738860).",
        )
        w.stamp_meta(
            existing,
            {
                PRELAND_STOP_KEY: {
                    "node": t.id,
                    "reason": reason,
                    "head": head,
                    "repeats": repeats,
                }
            },
        )
        if not any(b.id == existing.id for b in plan.blockers.get(t.id, [])):
            w.store.add_link(
                src_ref_id=t.id, dst_ref_id=existing.id, relation="blocked-by"
            )
            plan.blockers.setdefault(t.id, []).append(
                _Blocker(id=existing.id, status="open", is_ask=True, created_at=now)
            )
        res.notes.append(
            f"td{t.id}: pre-land review loop stopped again ({why}) — "
            f"repeat {repeats} noted on ask td{existing.id}, no new ask"
        )
        return
    res.asks += 1
    if w.dry_run:
        # Mirror the hold so the dispatch step mints no rebuild this pass.
        plan.blockers.setdefault(t.id, []).append(
            _Blocker(id=-t.id, status="open", is_ask=True, created_at=now)
        )
        res.notes.append(f"td{t.id}: would ask coordinator — {why}")
        return
    ask_id = _mint_coordinator_ask(
        w.store,
        plan,
        t,
        title=(
            f"td{t.id}: pre-land review loop stopped ({reason}) — a coordinator call"
        ),
        body=(
            f"The plan tick stopped rebuilding td{t.id} "
            f"({t.title.splitlines()[0] if t.title else ''}): {why}. "
            f"Stop reason: {reason} — {_PRELAND_STOPS[reason]}.\n\n"
            f"Newest verdict (review td{n.id}):\n{kind} — {rest}\n\n"
            f"The branch is on origin and td{t.id} is open, blocked by "
            "this ask. Read the branch against the finding, then leave a "
            f"`brief:` comment on td{t.id} (what to change) or on review "
            f"td{n.id} (how to verify what is already there) and close "
            f"this ask `acknowledged` — td{t.id} rebuilds and the round "
            f"count starts over. To drop the work, close td{t.id} "
            "STATUS:won't-do."
        ),
        context=why,
        now=now,
        block_node=True,
        extra_meta={
            PRELAND_STOP_KEY: {
                "node": t.id,
                "reason": reason,
                "head": head,
                "repeats": 0,
            }
        },
    )
    w.comment(
        t,
        f"plan_dispatch: {why} — no rebuild mints; held on ask td{ask_id} (gr684815).",
    )
    res.notes.append(f"td{t.id}: pre-land review loop stopped — ask td{ask_id} ({why})")


def _review_back_to_builder(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    kind: str,
    rest: str,
    now: datetime,
    raw: str = "",
) -> None:
    """A pre-land FIX/REVERT (gr570506, Reto 2026-11-12: "if it says no,
    the work goes back to the builder"). Nothing landed, so no fix or
    revert task mints: each held target's ``meta.pending_land`` clears
    into ``meta.preland_held`` (gr681208 — the stash a rebuild's
    ``validated:``/``no-op:`` verdict re-holds from, since that verdict
    is "the pushed branch is ready as it stands", not "nothing to
    land"), it reopens with the findings as its ``handoff:`` — the next
    build's brief, and only the NEWEST finding — and the reviewer stays
    open for the rebuild (its ``blocked-by`` edge re-arms while the
    target is open and releases when the next build defers again). The
    rounds are counted (gr684815): the target is held on a coordinator
    ask only on the failure the ask describes (gr688058) — the same
    finding twice running, an unchanged reviewed head, a verdict that
    names a head other than the pushed tip, or
    :data:`PRELAND_FIX_CAP` rounds. The stash carries ``at``, the
    send-back time (gr738860): :func:`_step_verdicts` reads only a
    verdict newer than it, so the verdict this review just judged cannot
    re-hold the land before the rebuild runs. The pushed head goes to
    :func:`_park_preland_rounds` for its one-ask-per-slice record."""
    targets = _pending_targets(plan, n)
    single = len(targets) == 1
    ruled: list[int] = []
    for t in targets:
        held = t.meta.get(_templates.PENDING_LAND_KEY)
        pushed = str(held.get("head") or "") if isinstance(held, dict) else ""
        w.stamp_meta(
            t,
            {
                _templates.PRELAND_HELD_KEY: {
                    "params": t.meta[_templates.PENDING_LAND_KEY],
                    "reviewer": n.id,
                    # When the send-back happened (gr738860): a verdict
                    # older than this is the one that was reviewed, never
                    # the rebuild's. Microsecond precision — whole seconds
                    # would read a verdict posted just before the
                    # send-back as newer than it.
                    "at": now.astimezone(UTC).isoformat(),
                },
                _templates.PENDING_LAND_KEY: None,
            },
        )
        claims = _claims(t)
        if claims:
            w.remove_tags(t, claims)
        if t.status in _RULING_STATUSES:
            # gr663909: a coordinator ruling set while the review ran
            # outranks the verdict — no handoff, no reopen. The cleared
            # hold lets the dead-chain sweep close this review.
            ruled.append(t.id)
            w.comment(
                t,
                f"plan_dispatch: review {kind} — {rest}\n\n(review td{n.id}, "
                f"pre-land — but td{t.id} is STATUS:{t.status}, a ruling "
                "set while the review ran; it stands and the verdict "
                "reopens nothing)",
            )
            continue
        # The rejected build burns no attempt (``_review_revert``'s
        # floor): the rebuild dispatches on a fresh budget.
        ended = [j for j in plan.jobs(t) if j.status in _JOB_ENDED]
        if ended:
            w.stamp_meta(t, {"rearmed_after_job": ended[-1].id})
        w.comment(
            t,
            f"handoff: review: {kind} — {rest}\n\n(review td{n.id}, "
            f"{_templates.PRELAND_HANDBACK_MARK} — rebuild on it "
            "from the brief with this finding in mind)",
        )
        if t.status != "open":
            w.set_status(t, "open")
        cstate, _ck, cjob = _conflict_view(plan, t)
        if cstate == "wait" and cjob is not None and cjob.status == "succeeded":
            # gr741695: the verdict judged a conflict resolution. It is a
            # failed try — the next conflict attempt mints from it — and
            # not a pre-land round: the tries are the bound
            # (:data:`CONFLICT_MAX_TRIES`), so :data:`PRELAND_FIX_CAP` and
            # the same-finding checks do not count it.
            tries = _dict_of(t.meta.get(CONFLICT_TRIES_KEY))
            w.stamp_meta(
                t,
                {
                    CONFLICT_TRIES_KEY: {**tries, "sent_back": cjob.id},
                    _templates.PRELAND_FIX_ROUNDS_KEY: None,
                },
            )
            res.notes.append(
                f"td{t.id}: resolution review {kind} — conflict try {_ck} failed"
            )
            continue
        count, record, stop = _preland_round(n, t, kind, rest, pushed, raw, single, now)
        if stop is not None:
            _park_preland_rounds(
                plan, w, res, n, t, kind, rest, stop, now, pushed, record
            )
        else:
            w.stamp_meta(t, {_templates.PRELAND_FIX_ROUNDS_KEY: record})
    sent_back = [t.id for t in targets if t.id not in ruled]
    res.notes.append(
        f"td{n.id}: review {kind} pre-land — back to the builder: "
        + (", ".join(f"td{i}" for i in sent_back) or "nothing")
        + (f"; STATUS stands on {', '.join(f'td{i}' for i in ruled)}" if ruled else "")
    )


def _rejected_head(t: _Node, params: Any) -> str:
    """The head the held land ``params`` would ship when it is the one
    the last pre-land send-back of ``t`` rejected
    (``meta.preland_fix_rounds.head``, gr762396), else ``""``."""
    head = str(params.get("head") or "") if isinstance(params, dict) else ""
    rounds = _dict_of(t.meta.get(_templates.PRELAND_FIX_ROUNDS_KEY))
    rejected = str(rounds.get("head") or "")
    return head if head and rejected and _same_sha(head, rejected) else ""


def _rejected_at(plan: _Plan, t: _Node) -> datetime | None:
    """When ``t``'s kept pre-land record was last rejected or stopped
    (gr762396): ``stopped_at`` (the park), else ``at`` (the send-back);
    only a record stamped before either existed falls back to the
    newest pre-land-stop ask on ``t``."""
    rounds = _dict_of(t.meta.get(_templates.PRELAND_FIX_ROUNDS_KEY))
    for key in ("stopped_at", "at"):
        try:
            return datetime.fromisoformat(str(rounds[key]))
        except (KeyError, TypeError, ValueError):
            continue
    asks = [
        m.created_at
        for m in plan.nodes.values()
        if m.kind == "todo"
        and _dict_of(m.meta.get(PRELAND_STOP_KEY)).get("node") == t.id
    ]
    return max(asks, default=None)


def _rehold_exemption(store: Any, plan: _Plan, n: _Node, t: _Node) -> str | None:
    """What buys the rejected head one more review (Reto 2026-11-22,
    gr762396), newer than :func:`_rejected_at`: ``"brief"`` — a
    coordinator ``brief:`` on the reviewer ``n`` ("how to verify what is
    already there"); ``"validated"`` — the builder's ``validated:``
    rebuttal on ``t`` (the self-correction path); else ``None``. Once
    per record: the review it buys writes a new record (``at``) or a
    new stop (``stopped_at``), newer than the evidence."""
    since = _rejected_at(plan, t)
    if since is None:
        return None
    from precis.workers.job_types import run_slice as _rs

    brief_at = getattr(
        _newest_brief_chunk(n, _rs._todo_chunks(store, n.id)), "created_at", None
    )
    if isinstance(brief_at, datetime) and brief_at > since:
        return "brief"
    verdict = _verdict_comment(store, t.id, since=since)
    if verdict is not None and verdict[0] == "validated":
        return "validated"
    return None


def _held_on_rejected_head(
    store: Any, plan: _Plan, w: _Writer, n: _Node, t: _Node, params: Any
) -> str:
    """The head to park ``t`` on instead of releasing the reviewer ``n``
    (gr762396): :func:`_rejected_head`, but only when ``n`` is the review
    that wrote the kept record (``reviewer``; a record without one is
    taken as ``n``'s) — a re-minted review node judges afresh — and no
    :func:`_rehold_exemption` stands. An exemption is marked on the
    record (``exempt``) for :func:`_preland_round` and returns ``""``."""
    head = _rejected_head(t, params)
    if not head:
        return ""
    rounds = _dict_of(t.meta.get(_templates.PRELAND_FIX_ROUNDS_KEY))
    wrote = rounds.get("reviewer")
    if isinstance(wrote, int) and wrote != n.id:
        return ""
    exempt = _rehold_exemption(store, plan, n, t)
    if exempt is None:
        return head
    if rounds.get("exempt") != exempt:
        w.stamp_meta(
            t, {_templates.PRELAND_FIX_ROUNDS_KEY: {**rounds, "exempt": exempt}}
        )
    return ""


def _park_rejected_head(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    t: _Node,
    head: str,
    now: datetime,
    denied: str | None = None,
) -> None:
    """Hold ``t`` on the "unchanged head" stop instead of releasing the
    reviewer ``n`` onto ``head`` — the head its last FIX/REVERT already
    rejected (gr762396: five FIX rounds on one head, td716268). A session
    that adds nothing re-holds the rejected head (the EMPTY-push
    continue, the ahead-branch route, a ``validated:``/``no-op:``
    re-hold), and a review of an unchanged tree only repeats itself. A
    standing ``pending_land`` moves to the ``meta.preland_held`` stash
    and ``t`` reopens, as a send-back leaves it, so the reviewer stays
    blocked until a rebuild defers a new head; the ask comes from
    :func:`_park_preland_rounds`, whose comment names the head."""
    record = _dict_of(t.meta.get(_templates.PRELAND_FIX_ROUNDS_KEY))
    held = t.meta.get(_templates.PENDING_LAND_KEY)
    if isinstance(held, dict):
        w.stamp_meta(
            t,
            {
                _templates.PRELAND_HELD_KEY: {
                    "params": held,
                    "reviewer": n.id,
                    "at": now.astimezone(UTC).isoformat(),
                },
                _templates.PENDING_LAND_KEY: None,
            },
        )
        claims = _claims(t)
        if claims:
            w.remove_tags(t, claims)
        if t.status != "open":
            w.set_status(t, "open")
    elif denied:
        # The verdict step found a no-op while the branch was still stashed.
        # Consume that verdict so answering the ask can start a fresh run.
        stash = _dict_of(t.meta.get(_templates.PRELAND_HELD_KEY))
        w.stamp_meta(
            t,
            {
                _templates.PRELAND_HELD_KEY: {
                    **stash,
                    "at": now.astimezone(UTC).isoformat(),
                }
            },
        )
    kind = str(record.get("kind") or "")
    rest = str(record.get("verdict") or "")
    if not kind or not rest:
        # A record stamped before the verdict was kept as written.
        kind, _, rest = str(record.get("text") or "").partition(" ")
    why = (
        f"review td{n.id} already sent the branch back on head {head[:12]}, "
        "and the held land is that unchanged head — the rebuild pushed "
        "nothing new, so no review re-runs on it (gr762396)"
    )
    stop = ("denied path", denied) if denied else ("unchanged head", why)
    _park_preland_rounds(
        plan,
        w,
        res,
        n,
        t,
        kind.upper() or "FIX",
        rest or "(the rejected finding's text is not recorded)",
        stop,
        now,
        head,
        record,
    )


def _denied_noop_reason(store: Any, plan: _Plan, w: _Writer, t: _Node) -> str | None:
    finding = _handback_finding(store, plan, t, w, after_rebuild=True)
    if finding is None:
        return None
    denied = _route.coordinator_route(
        _as_intent(finding), _node_deny_paths(store, plan, t), graph=False
    )
    return f"the no-op rebuild's review finding {denied}" if denied else None


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
        if plan.child_todos(n) or plan.live_job(n) or _preland_review_held(plan, n):
            continue
        # run_slice can re-hold the land and mark the build done itself.
        # Check that path before accepting a verdict or releasing a review.
        parked = False
        for t in _pending_targets(plan, n) if not _closed(n) else ():
            build_jobs = plan.jobs(t)
            if (
                t.status != "done"
                or not build_jobs
                or build_jobs[-1].status != "succeeded"
                or plan.live_job(t)
            ):
                continue
            verdict = _verdict_comment(store, t.id, since=build_jobs[-1].created_at)
            if verdict is None or verdict[0] != "no-op":
                continue
            denied = _denied_noop_reason(store, plan, w, t)
            if denied is not None:
                params = _dict_of(t.meta.get(_templates.PENDING_LAND_KEY))
                _park_rejected_head(
                    plan, w, res, n, t, str(params.get("head") or ""), now, denied
                )
                parked = True
        if parked:
            continue
        dead = _dead_land_targets(plan, store, n)
        if dead:
            _close_dead_chain(plan, w, res, n, dead, now)
            continue
        if _closed(n):
            continue
        jobs = plan.jobs(n)
        seen = n.meta.get("review")
        unread = bool(
            jobs
            and jobs[-1].status == "succeeded"
            and not _voided(jobs[-1])
            and not (isinstance(seen, dict) and seen.get("job") == jobs[-1].id)
        )
        if not unread:
            # gr762396: the reviewer is about to be released onto a held
            # land — never onto the head its last send-back rejected,
            # unless a newer `brief:` on the reviewer or `validated:` on
            # the build buys it one review.
            rejected = [
                (t, sha)
                for t in _pending_targets(plan, n)
                if t.status not in _RULING_STATUSES
                and (
                    sha := _held_on_rejected_head(
                        store, plan, w, n, t, t.meta.get(_templates.PENDING_LAND_KEY)
                    )
                )
            ]
            for t, sha in rejected:
                _park_rejected_head(plan, w, res, n, t, sha, now)
            if rejected:
                continue
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
        if isinstance(seen, dict) and seen.get("job") == newest.id:
            continue
        verdict, raw = _read_verdict(store, n.id, newest.created_at)
        if verdict is not None and verdict[0] == "LAND":
            # gr741695: a pending conflict resolution lands first. The
            # check comes before any claim or status change, so a held
            # reviewer keeps the state its run left (claimed, ``doing``)
            # for the whole hold instead of sitting ``doing`` with its
            # claim dropped. The verdict stays unrecorded: the next pass
            # reads it again and mints the held land once the hold is gone
            # or has lapsed.
            waits = [
                h
                for t in _pending_targets(plan, n)
                if (h := _conflict_land_note(plan, store, res, t, now)) is not None
            ]
            if waits:
                res.notes.append(
                    f"td{n.id}: review LAND for td{_review_from(n) or n.id} — "
                    f"the held land waits for td{waits[0]}'s conflict resolution"
                )
                continue
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
            w.stamp_meta(n, {"review": {**record, "verdict": None}})
            cap = _retry_cap(
                n, _cap_rungs(plan, lane_table, n, job_type_, params_, rules)
            )
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
        # gr761723: the build run(s) the verdict judged, read before the
        # verdict's handling clears the held land that names them.
        judged_builds = _reviewed_build_jobs(plan, n, newest)
        marker = _templates.REVIEW_READONLY_MARKER
        pending = _pending_targets(plan, n)
        preland = bool(pending) and len(pending) == len(_review_targets(plan, n))
        if kind == "LAND":
            single = len(pending) == 1
            judged = _verdict_heads(raw or "") if single else []
            stale_reads: list[tuple[_Node, list[str], str]] = []
            for t in pending:
                held = t.meta.get(_templates.PENDING_LAND_KEY)
                pushed = str(held.get("head") or "") if isinstance(held, dict) else ""
                if judged and pushed and not any(_same_sha(h, pushed) for h in judged):
                    stale_reads.append((t, judged, pushed))
                else:
                    _mint_preland_land(store, plan, w, res, n, t)
            if stale_reads:
                # td730820: the verdict named a head the held stamp does
                # not hold — it read a different commit than the mint
                # would ship, so the LAND releases nothing. The hold
                # stands; the reviewer re-runs with a fresh read, bounded
                # by the retry cap like a malformed verdict.
                job_type_, params_ = _resolve_op(n)
                cap = _retry_cap(
                    n, _cap_rungs(plan, lane_table, n, job_type_, params_, rules)
                )
                detail = "; ".join(
                    f"td{t.id}: judged {heads[0][:12]} but the pushed tip "
                    f"is {pushed[:12]}"
                    for t, heads, pushed in stale_reads
                )
                w.stamp_meta(n, {"review": {**record, "verdict": None}})
                w.comment(
                    n,
                    f"plan_dispatch: job:{newest.id} posted `review: LAND` "
                    f"naming a judged head that is not the held land's "
                    f"pushed tip ({detail}) — the verdict read a different "
                    "commit than the hold would ship, so the land does not "
                    "mint and the hold stands (td730820). Retrying "
                    f"(retry cap {cap}), then rescue.",
                )
                if n.status != "open":
                    w.set_status(n, "open")
                res.notes.append(
                    f"td{n.id}: review LAND for {handle} {marker} read a "
                    "stale head — the held land stays held; reviewer "
                    "re-runs (td730820)."
                )
                continue
            w.set_status(n, "done")
            res.notes.append(f"td{n.id}: review LAND for {handle} {marker}; closed")
        elif preland and len(pending) == 1 and _ALREADY_ON_MAIN.search(rest):
            target = pending[0]
            if target.status not in _RULING_STATUSES:
                w.comment(target, f"handoff: moot: review td{n.id} — {rest}")
                w.add_tag(target, MOOT_TAG, now)
                w.remove_tags(target, _claims(target))
                w.set_status(target, "done")
            w.stamp_meta(
                target,
                {
                    _templates.PENDING_LAND_KEY: None,
                    _templates.PRELAND_HELD_KEY: None,
                    _templates.PRELAND_FIX_ROUNDS_KEY: None,
                },
            )
            w.set_status(n, "won't-do")
            res.notes.append(f"td{n.id}: review {kind} — duplicate work on main")
        elif preland:
            # gr570506 (Reto 2026-11-12): a FIX/REVERT on a branch that
            # has not landed sends the work back to the builder — nothing
            # is on main to fix or revert — and the reviewer stays open
            # for the rebuild it then re-gates.
            _review_back_to_builder(store, plan, w, res, n, kind, rest, now, raw or "")
            if all(_closed(t) for t in _review_targets(plan, n)):
                # gr663909: every held target was ruled closed while the
                # review ran — nothing is left to re-gate, and an open
                # reviewer would re-dispatch over terminal blockers.
                w.set_status(n, "won't-do")
                res.notes.append(
                    f"td{n.id}: review {kind} for {handle} {marker}; "
                    "every held target was ruled closed — the review "
                    "closes with it"
                )
            else:
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
            elif (rts := _review_targets(plan, n)) and all(_closed(t) for t in rts):
                # gr663909: every requeue target was ruled closed while
                # the review ran — no rebuild comes to re-gate, and an
                # open reviewer would re-review the same land and
                # re-mint the revert in a loop. (An out-of-plan ``from``
                # has no targets — it waits on the revert task itself,
                # gr597625.)
                w.set_status(n, "won't-do")
                res.notes.append(
                    f"td{n.id}: review REVERT for {handle} {marker}; "
                    f"minted td{revert_id} — every requeue target was "
                    "ruled closed, the review closes with it"
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
        if not w.dry_run:
            _stamp_review_signal(store, judged_builds, kind, newest.id)
        w.stamp_meta(n, {"review": record})
        res.reviews += 1


def _reviewed_build_jobs(plan: _Plan, n: _Node, review: _Node) -> list[int]:
    """The build job each of ``n``'s targets was judged on (gr761723) by
    the ``review`` job: the build job of the attempt its launch named
    (:data:`~precis.plans.templates.REVIEWED_ATTEMPT_KEY`, td763090) —
    the held land may name a replacement build by the time the verdict
    is read. Without that record (a job launched before it, a landed
    squash, a slicer's slices): the job a held land's attempt ref names
    (``refs/factory/build/<todo>/<job>``, :func:`outcome.build_job_of_ref`)
    when that build predates the review, else the target's newest
    ``run_slice`` job before the review that left commits
    (``meta.branch_commits``), else its newest laned one."""
    from precis.plans import outcome as _outcome

    review_job = review.id
    seen = (review.meta or {}).get(_templates.REVIEWED_ATTEMPT_KEY)
    seen = seen if isinstance(seen, dict) else {}
    out: list[int] = []
    for t in _review_targets(plan, n):
        job: int | None = None
        if seen.get("todo") == t.id:
            named = seen.get("job")
            job = named if isinstance(named, int) and named > 0 else None
        else:
            for key in (_templates.PENDING_LAND_KEY, _templates.PRELAND_HELD_KEY):
                held = t.meta.get(key)
                held = held.get("params", held) if isinstance(held, dict) else None
                if isinstance(held, dict):
                    job = _outcome.build_job_of_ref(held.get("ref"))
                if job is not None and job >= review_job:
                    # td763090: a build minted after the review is a
                    # replacement it never read.
                    job = None
                if job is not None:
                    break
        if job is None:
            builds = [
                j
                for j in plan.jobs(t)
                if j.meta.get("job_type") == DEFAULT_OP
                and j.meta.get("lane")
                and j.id < review_job
            ]
            pick = [j for j in builds if j.meta.get("branch_commits")] or builds
            job = max((j.id for j in pick), default=None)
        if job is not None:
            out.append(job)
    return out


def _stamp_review_signal(store: Any, jobs: list[int], verdict: str, by: int) -> None:
    """Record a review verdict on the build jobs it judged — the
    suitability ledger's validated signal for their model (gr761723).
    Best-effort: a failed stamp loses one ledger outcome, never the
    verdict's handling."""
    from precis.plans import outcome as _outcome

    for job in jobs:
        try:
            _outcome.record_build_signal(store, job, "review", verdict, by_job=by)
        except Exception:
            log.warning(
                "plan_dispatch: review signal on job %d failed", job, exc_info=True
            )


# ── (2b) todo briefs ───────────────────────────────────────────────

#: ``meta`` key recording the brief job the tick last read, ``{job, at,
#: outcome}``: a pass never reads one job twice (``meta.review``'s rule).
_BRIEF_READ_KEY = "brief_read"


def _is_brief_node(n: _Node) -> bool:
    """A ``brief`` generator: one session whose final message is a builder
    brief or a refusal (:func:`_step_todo_briefs`)."""
    gen = n.meta.get("generate")
    return (
        node_kind(n.meta) == "generator"
        and isinstance(gen, dict)
        and gen.get("role") == "brief"
    )


def _source_todo_of(plan: _Plan, n: _Node) -> int | None:
    """The source todo id ``n``'s plan root carries in ``meta.source_todo``,
    ``None`` for any chain not minted from a todo (a gripe chain)."""
    root = plan.nodes.get(n.root)
    raw = root.meta.get(SOURCE_TODO_KEY) if root is not None else None
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _brief_builds(plan: _Plan, n: _Node) -> list[_Node]:
    """The build node(s) of the chain brief node ``n`` heads: the task nodes
    under its root that are ``blocked-by`` it."""
    return [
        m
        for m in plan.todos()
        if m.root == n.root
        and node_kind(m.meta) == "task"
        and any(b.id == n.id for b in plan.blockers.get(m.id, []))
    ]


def _brief_handoff(store: Any, n: _Node, job: _Node) -> str | None:
    """The ``handoff:`` comment THIS brief job posted when its session
    ended. ``run_slice`` signs the comment with the job's id, so the
    outcome binds to the job: a later ``handoff:`` comment — a human's
    ordinary note, however new — is someone else's words and decides
    nothing. The job record keeps only a clipped tail, so the comment
    is the one place the message lives."""
    for c in _comments(store, n.id):
        if (getattr(c, "meta", None) or {}).get(HANDOFF_JOB_KEY) != job.id:
            continue
        if str(c.text).lstrip().lower().startswith("handoff:"):
            return str(c.text)
    return None


def _awaits_brief(plan: _Plan, n: _Node, w: _Writer) -> str | None:
    """The hold under which a source-todo build waits before it may
    dispatch: ``"missing"`` — no ``brief:`` comment reaches it (whatever
    its blockers say: a human or another step may have closed the brief
    node, and it never dispatches without one); ``"stale"`` — the newest
    ``brief:`` carries a ``Read:`` stamp the live source has moved past,
    so the chain re-briefs instead of building
    (:func:`_rebrief_stale_brief`); ``None`` — it may run. Only the node
    ``blocked-by`` a brief node counts — a fix or revert task the chain
    mints later builds from its own brief. A dry pass reads this pass's
    pending comments too: the brief step ran earlier in the same pass,
    so a live tick's post-and-dispatch reads as one here as well."""
    source = _source_todo_of(plan, n)
    if node_kind(n.meta) != "task" or source is None:
        return None
    if not any(
        (m := plan.nodes.get(b.id)) is not None and _is_brief_node(m)
        for b in plan.blockers.get(n.id, [])
    ):
        return None
    from precis.workers.job_types import run_slice as _rs

    pending = [
        text
        for ref_id, text in w.dry_comments
        if ref_id == n.id and text.lstrip().lower().startswith("brief:")
    ]
    if pending:
        newest = pending[-1]
    else:
        texts = [
            str(c.text or "")
            for c in _rs._todo_chunks(w.store, n.id)
            if c.chunk_kind == _COMMENT_KIND
            and str(c.text or "").lstrip().lower().startswith("brief:")
        ]
        if not texts:
            return "missing"
        newest = texts[-1]
    read = next(
        (
            parsed
            for line in newest.splitlines()[:2]
            if (parsed := parse_read_line(line)) is not None and parsed[0] == source
        ),
        None,
    )
    if read is None:
        return None  # a hand-posted brief carries no freshness claim
    return "stale" if read[1] != source_updated_at(w.store, source) else None


def _rebrief_stale_brief(plan: _Plan, n: _Node, w: _Writer, res: PassResult) -> None:
    """The build's newest brief names a source revision the source has
    moved past: reopen its brief node so a fresh session re-reads the
    source — the build is ``blocked-by`` it again and waits — and mark
    why. Idempotent across passes: a brief node already open is left
    alone. :func:`post_brief` replaces the stale comment when the fresh
    brief lands, so a builder never reads it."""
    brief_node = next(
        (
            m
            for b in plan.blockers.get(n.id, [])
            if (m := plan.nodes.get(b.id)) is not None and _is_brief_node(m)
        ),
        None,
    )
    if brief_node is None or brief_node.status == "open":
        return
    w.set_status(brief_node, "open")
    w.comment(
        brief_node,
        f"plan_dispatch: td{n.id} held — the source changed after this "
        "brief was written; re-briefing. The stale brief on the build is "
        "replaced when the fresh one lands.",
    )
    res.notes.append(
        f"td{n.id}: source changed after its brief — re-briefing td{brief_node.id}"
    )


def _step_todo_briefs(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Read a brief session's final message (the ``handoff:`` comment the
    newest brief job posted; the comment's chunk ``meta`` binds it to that
    job, so a later ``handoff:`` comment decides nothing) for every
    ``brief`` generator of a source-todo
    chain whose newest job succeeded; :func:`precis.plans.todo_chain.parse_brief`
    decides the outcome from its first line. A brief naming this chain's build
    node is copied onto it (``post_brief``), carrying the ``Read:`` stamp —
    the source revision the writer read — and the brief node closes done, in
    one transaction. A refusal files the one ask on the source, tags it and
    closes the chain with no build (``refuse_source``; the rest of the chain
    closes here, in the same pass, so nothing dispatches behind it). A split
    creates the part todos and closes the chain the same way with no ask
    (``split_source``). A ``built`` refusal closes the source done and the
    chain the same way with no ask (``close_built``; a source that is no
    longer open, has an open child, is held or whose evidence names no
    handle, sha or file gets the ordinary ask). Any other
    ending — no message, a clipped one, neither first line, a brief naming
    another node, a ``Read:`` line naming the wrong source or a stamp newer
    than the source itself — is a failed attempt: the node reopens for a
    retry, and the succeeded job itself is the attempt :func:`_attempts`
    counts, so the cap, rescue and ask are the ordinary ones. A brief whose
    stamp the live source has already moved past is posted anyway and the
    dispatch gate re-briefs the chain. The node never closes done without a
    brief posted, and the build never unblocks. A gripe chain carries no
    ``meta.source_todo`` and is never touched."""
    for n in plan.todos():
        if not _is_brief_node(n):
            continue
        try:
            _todo_brief_node(store, plan, w, res, now, lane_table, rules, n)
        except Exception:
            log.warning("plan_dispatch: brief td%s outcome failed", n.id, exc_info=True)
            res.notes.append(
                f"td{n.id}: brief outcome failed — left unread; the next pass retries"
            )


def _todo_brief_node(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime,
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None,
    n: _Node,
) -> None:
    """Handle one brief node (:func:`_step_todo_briefs`)."""
    if not _is_brief_node(n) or plan.paused(n) or _closed(n):
        return
    source = _source_todo_of(plan, n)
    if source is None or plan.child_todos(n) or plan.live_job(n):
        return
    jobs = plan.jobs(n)
    if not jobs or jobs[-1].status != "succeeded":
        return
    newest = jobs[-1]
    if _voided(newest):
        # The seat voided the run by hand (gr568824): it counts for
        # nothing — drop its dead claim and reopen so the node
        # re-dispatches, and never read its comments as an outcome.
        w.remove_tags(n, _claims(n))
        if n.status != "open":
            w.set_status(n, "open")
        res.notes.append(f"td{n.id}: brief job:{newest.id} voided")
        return
    seen = n.meta.get(_BRIEF_READ_KEY)
    if isinstance(seen, dict) and seen.get("job") == newest.id:
        return
    raw = _brief_handoff(store, n, newest)
    outcome = parse_brief(raw) if raw is not None else None
    builds = _brief_builds(plan, n)
    build = builds[0] if len(builds) == 1 else None
    record: dict[str, Any] = {"job": newest.id, "at": _iso(now)}
    live_stamp = source_updated_at(store, source)
    if isinstance(outcome, Brief) and (build is None or outcome.build_id != build.id):
        what = f"`brief: td{outcome.build_id}`, but " + (
            f"this chain's build node is td{build.id}"
            if build is not None
            else "no single build node waits on this brief"
        )
    elif isinstance(outcome, Brief) and outcome.read[0] != source:
        what = (
            f"a brief whose `Read:` line names td{outcome.read[0]}, not "
            f"the source td{source}"
        )
    elif isinstance(outcome, Brief) and outcome.read[1] > live_stamp:
        what = (
            f"a brief stamped {outcome.read[1]} — newer than the source's "
            f"own updated_at ({live_stamp}); the stamp is garbled"
        )
    elif outcome is None:
        what = (
            "no `handoff:` comment"
            if raw is None
            else "a clipped message (the brief outgrew the handoff cap)"
            if CLIP_MARKER in raw
            else f"a malformed split ({_first_line(raw)!r})"
            if re.match(r"\s*(?:handoff:\s*)?split:", raw, re.IGNORECASE)
            else f"neither a brief nor a refusal ({_first_line(raw)!r})"
        )
    else:
        what = ""
    if what:
        job_type_, params_ = _resolve_op(n)
        cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type_, params_, rules))
        w.remove_tags(n, _claims(n))
        w.stamp_meta(n, {_BRIEF_READ_KEY: {**record, "outcome": None}})
        w.comment(
            n,
            f"plan_dispatch: job:{newest.id} succeeded with {what}; want "
            f"{BRIEF_GRAMMAR}. Retrying (retry cap {cap}), then rescue.",
        )
        if n.status != "open":
            w.set_status(n, "open")
        res.notes.append(f"td{n.id}: brief job:{newest.id} left {what}")
        return
    claims = _claims(n)
    if isinstance(outcome, Brief):
        assert build is not None  # ``what`` is set otherwise
        record["outcome"] = "brief"
        _close_brief_node(store, w, n, record, claims, post=(build, source, outcome))
        res.notes.append(f"td{n.id}: brief posted on td{build.id}; closed")
        return
    split: Split | None = None
    if isinstance(outcome, Split):
        refused = _refuse_split(store, source, outcome)
        if refused is None:
            split = outcome
        else:
            outcome = refused
    ask_id: int | None = None
    part_ids: list[int] = []
    withdrawn = False
    built = False
    from precis.workers.executors._common import source_todo_holds

    # The source may have changed while the brief ran: the build path's holds
    # apply to a split and to a built close too (both read them under the
    # source's row lock).
    def hold(c: Any) -> str | None:
        return source_todo_holds(c, [n.id]).get(n.id)

    if split is not None:
        if not w.dry_run:
            part_ids = split_source(store, n.root, source, n.id, split, hold=hold)
            withdrawn = not part_ids
        record["outcome"] = (
            "split-withdrawn" if withdrawn else f"split:{len(split.parts)}"
        )
        ended = (
            f"the split of td{source} was not applied because the source "
            "todo changed while the brief ran"
            if withdrawn
            else f"the brief for td{source} was split into {len(split.parts)} parts"
        )
    else:
        assert isinstance(outcome, Refusal)  # ``what`` is set otherwise
        if outcome.reason == "built" and not w.dry_run:
            built = close_built(store, n.root, source, n.id, outcome.why, hold=hold)
        record["outcome"] = "built-closed" if built else f"refuse:{outcome.reason}"
        if built:
            ended = f"td{source} was closed as already built"
        else:
            if not w.dry_run:
                ask_id = refuse_source(
                    store, n.root, source, outcome.reason, outcome.why
                )
            ended = f"the brief for td{source} was refused ({outcome.reason})"
    root = plan.nodes[n.root]
    root.status = "won't-do"
    closed = 0
    for m in plan.todos():
        if m.root != n.root or m.id in (n.id, n.root) or _closed(m):
            continue
        if plan.live_job(m) or _is_ask_node(m):
            continue
        w.comment(m, f"plan_dispatch: plan closed — {ended}; no build")
        w.set_status(m, "won't-do")
        closed += 1
    # Last: the brief node is never done while the build is still open.
    _close_brief_node(store, w, n, record, claims, post=None)
    res.phases_closed += closed
    if withdrawn:
        res.notes.append(
            f"td{n.root}: split of source td{source} not applied (source todo "
            f"changed while the brief ran); plan td{n.root} closed won't-do, "
            f"{closed} node(s) with it"
        )
        return
    if split is not None:
        res.notes.append(
            f"td{n.root}: source td{source} split into {len(split.parts)} parts"
            + (f" ({', '.join(f'td{i}' for i in part_ids)})" if part_ids else "")
            + f"; plan td{n.root} closed won't-do, {closed} node(s) with it"
        )
        return
    if built:
        res.notes.append(
            f"td{n.root}: source td{source} closed as already built; plan "
            f"td{n.root} closed won't-do, {closed} node(s) with it"
        )
        return
    assert isinstance(outcome, Refusal)
    if w.dry_run and outcome.reason == "built":
        res.notes.append(
            f"td{n.id}: brief refused (built) — would close as already built "
            f"or ask on td{source}; plan td{n.root} closed won't-do, "
            f"{closed} node(s) with it"
        )
        return
    res.asks += 1
    res.notes.append(
        f"td{n.id}: brief refused ({outcome.reason}) — "
        + (f"asked in td{ask_id} on " if ask_id is not None else "would ask on ")
        + f"td{source}, tagged needs-spec; plan td{n.root} closed won't-do, "
        f"{closed} node(s) with it"
    )


def _refuse_split(store: Any, source: int, split: Split) -> Refusal | None:
    """The refusal a split outcome turns into, or ``None`` when it stands:
    more than :data:`MAX_SPLIT_PARTS` parts, or a source that is itself a
    part (a part is never split again)."""
    n = len(split.parts)
    if n > MAX_SPLIT_PARTS:
        return Refusal(
            "split",
            f"the brief session split it into {n} parts; more than four needs "
            "the owner",
        )
    ref = store.get_ref(kind="todo", id=source)
    part = (getattr(ref, "meta", None) or {}).get(SPLIT_PART_KEY)
    if isinstance(part, dict):
        titles = "; ".join(p.title for p in split.parts)
        return Refusal(
            "split",
            f"this todo is part {part.get('index')} of td{part.get('source')} "
            "and a part is never split again; the session proposed "
            f"{n} parts: {titles}",
        )
    return None


def _close_brief_node(
    store: Any,
    w: _Writer,
    n: _Node,
    record: dict[str, Any],
    claims: list[str],
    *,
    post: tuple[_Node, int, Brief] | None,
) -> None:
    """Close brief node ``n`` done, drop its claim and stamp the read; with
    ``post`` — ``(build node, source id, brief)`` — the brief comment lands
    in the same transaction, so the node never closes without it. A dry
    pass queues the comment on the writer instead: later steps of the same
    pass read it like a landed one (gr738860)."""
    if w.dry_run:
        if post is not None:
            build, source, brief = post
            w.comment_id(
                build.id, brief_comment_text(source, n.id, brief.text, brief.read)
            )
    else:
        with store.tx() as conn:
            if post is not None:
                build, source, brief = post
                post_brief(
                    store, build.id, source, n.id, brief.text, brief.read, conn=conn
                )
            for value in claims:
                store.remove_tag(n.id, Tag.open(value), conn=conn)
            store.add_tag(
                n.id,
                Tag.closed("STATUS", "done"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            store.append_event(n.id, source=NAME, event="status:done", conn=conn)
            store.stamp_ref_meta(n.id, {_BRIEF_READ_KEY: record}, conn=conn)
    # The in-memory node follows the write, so a raise leaves it untouched.
    n.status = "done"
    for value in claims:
        n.open_tags.pop(value, None)
    n.meta.update({_BRIEF_READ_KEY: record})


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


def _answers_start_over(store: Any, ask_id: int) -> bool:
    """The closing answer on ask ``ask_id`` says ``start over`` (any case):
    the ``answer:`` comment, else the ask's newest comment. The ask's body
    names the option and so is never read."""
    answer = _release._newest_answer(store, ask_id)
    if answer is None:
        newest = _comments(store, ask_id)
        answer = str(newest[0].text) if newest else ""
    return "start over" in answer.lower()


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
        # Park = save, resume where the work is: the re-armed build
        # continues from the task's ``meta.build_ref`` (pinned to its
        # host) — unless the closing answer says ``start over``: the
        # next launch then deletes the task's refs in that host's hub,
        # clears ``meta.build_ref`` itself and cuts from the base. The
        # launch needs the record's host, so the re-arm never clears it.
        if _answers_start_over(w.store, newest_ask.id):
            stamp[_slice_park.START_OVER_KEY] = newest_ask.id
            stamp[_slice_park.PARKED_KEY] = None
            # A start-over rebuilds from the base: the conflict it was
            # parked on is gone with the work it conflicted.
            stamp[CONFLICT_KEY] = None
        # An answered ask buys the conflict path a fresh budget too.
        if n.meta.get(CONFLICT_TRIES_KEY) is not None:
            stamp[CONFLICT_TRIES_KEY] = None
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
        if n.status == "paused":
            # Resume lifts ``STATUS:paused`` (Reto 2026-11-16); a
            # ``waiting-for:*`` hold — waiting on files — stays as is.
            w.set_status(n, "open")
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
    transmits: the closure walks through it, it is just not re-closed.

    A ``repo_release`` node is a join, not a chain link (td659522 step
    3): the walk enters it only once ALL its non-ask blockers are dead —
    ``n`` itself, a node already walked, a ruled-out (non-``done``
    terminal) or ``verdict:moot`` node. One live blocker keeps it, and
    everything behind it, out; the walk re-tests it from each blocker
    it reaches, so a join whose last blocker dies later in the walk
    still closes."""
    dependents: dict[int, set[int]] = {}
    for src, targets in plan.blockers.items():
        for b in targets:
            dependents.setdefault(b.id, set()).add(src)
    seen = {n.id}
    reached = {n.id}
    frontier = [n.id]
    out: list[_Node] = []

    def blocker_dead(b: _Blocker) -> bool:
        node = plan.nodes.get(b.id)
        if b.is_ask or (node is not None and _is_ask_node(node)):
            return True  # an ask is a question, not work the join waits on
        if b.id in reached or plan.status_of(b) in _RULING_STATUSES:
            return True
        return node is not None and MOOT_TAG in node.open_tags

    while frontier:
        cur = frontier.pop()
        for dep_id in sorted(dependents.get(cur, ())):
            if dep_id in seen:
                continue
            m = plan.nodes.get(dep_id)
            if m is not None and m.kind == "todo" and _is_repo_release(m):
                live = [b for b in plan.blockers.get(dep_id, []) if not blocker_dead(b)]
                if live:
                    continue  # not seen: a later blocker may complete the join
            seen.add(dep_id)
            if m is None or m.kind != "todo" or m.root != n.root:
                continue
            reached.add(dep_id)
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
    cannot be read fails closed like :func:`_handoff_verdict`.
    Worker comments count only when bound to a job, as when run_slice
    relays the session's handoff. The dispatcher's own send-back can
    quote a verdict in the review finding. Newest first, first hit wins."""
    for c in _comments(store, todo_id):
        meta = getattr(c, "meta", None) or {}
        if str(meta.get("principal", "")).startswith("worker:") and not meta.get(
            HANDOFF_JOB_KEY
        ):
            continue
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


def _sent_back_at(store: Any, n: _Node, held: dict[str, Any]) -> datetime | None:
    """When a pre-land FIX/REVERT sent ``n`` back (gr738860): the
    stash's ``at`` stamp, else — a stash written before the stamp — the
    ``created_at`` of the newest comment on the node carrying a mark of
    :data:`_templates.PRELAND_HANDBACK_MARKS`. ``None`` when neither
    reads (the caller keeps the job-bounded read)."""
    raw = held.get("at")
    if isinstance(raw, str) and raw:
        try:
            at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            at = None
        if at is not None:
            return at if at.tzinfo else at.replace(tzinfo=UTC)
    for c in _comments(store, n.id):
        created = getattr(c, "created_at", None)
        if any(
            m in str(c.text) for m in _templates.PRELAND_HANDBACK_MARKS
        ) and isinstance(created, datetime):
            return created
    return None


def _step_verdicts(
    store: Any,
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    now: datetime | None = None,
) -> None:
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
    from — until a newer job exists (td604695). A launch refusal's
    clean meta is no all-clear either: the stamp is checked again on
    the source job the ``since`` bound reads off, so a denied session
    behind a refused retry still cannot close (td695902). A
    ``validated:``/
    ``no-op:`` on a build a pre-land review still gates is "the pushed
    branch is ready as it stands", never "nothing to land" (gr681208):
    the ``meta.preland_held`` stash a FIX/REVERT left re-holds the land
    and closes done as a fresh defer does; with neither hold nor stash
    the node stays open. The stash makes a verdict count only when it is
    NEWER than the send-back (gr738860, :func:`_sent_back_at`): no job
    runs between a FIX and its rebuild, so the job-bounded read kept
    finding the old ``no-op:`` the reviewer had just judged — it re-held
    and closed the node, the review ran on the same head, and every
    second round minted another pre-land-stop ask. An older verdict is
    left open with a note, and a ``validated:``/``no-op:`` on a stashed
    node held by an OPEN coordinator ask neither re-holds nor closes,
    and no verdict of any kind acts on a build an open pre-land stop
    holds (td763175) — each is ignored with a note.
    Nor does one whose stash is the head the last send-back rejected
    (gr762396): the re-hold would release the reviewer onto it in this
    pass, so the node parks on the "unchanged head" stop instead
    (:func:`_park_rejected_head`)."""
    for n in plan.todos():
        if _closed(n) or n.id == n.root or _is_ask_node(n):
            continue
        if _is_repo_release(n):
            # td659522 step 3: the tag on the remote is the result — a
            # release node closes only through its succeeded job.
            continue
        if _is_pin_bump(n):
            # td659522 step 4: a pin-bump node closes only through its
            # land_slice; no verdict line stands for the bump on main.
            continue
        if _is_brief_node(n):
            # A brief writer's only outcomes are a brief or a refusal
            # (:func:`_step_todo_briefs`): a ``no-op:``/``moot:``/
            # ``validated:`` line in its message would close it done —
            # unblocking the build with no brief — and a ``moot:`` would
            # kill the plan.
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
        # gr685988: the ``since`` bound must read off the newest job a
        # verdict could come FROM — an ended job that ran a session. A
        # launch refusal (``model_rule_refusal``/``host_refusal``/
        # ``attempt_free``/voided) never ran; letting its mint own the
        # bound disowns the earlier session's posted verdict — a
        # validate's `validated:` stayed masked behind the refused
        # retry the different-model rule filed, stranding the chain.
        src = next(
            (
                j
                for j in reversed(ended if ended else plan.jobs(n))
                if j.status in _JOB_ENDED
                and not _no_attempt(j)
                and j.meta.get(MODEL_RULE_KEY) is None
            ),
            None,
        )
        bound = src if src is not None else last
        # td695902 review: the stamp must ride the job the verdict is
        # read FROM, not only the newest ended one — a launch refusal
        # never ran, so ``last``'s clean meta says nothing about the
        # denied session sitting behind it. That session's posted
        # verdict is still the human-only ask's ruling: no close ahead
        # of it, exactly as the ``last`` check above rules when the
        # denied job is itself newest.
        if src is not None and src.meta.get(HUMAN_ONLY_KEY):
            continue
        held = n.meta.get(_templates.PRELAND_HELD_KEY)
        held_params = held.get("params") if isinstance(held, dict) else None
        held_land = isinstance(n.meta.get(_templates.PENDING_LAND_KEY), dict)
        since = bound.created_at if bound is not None else None
        sent_back: datetime | None = None
        if isinstance(held, dict) and isinstance(held_params, dict) and not held_land:
            # gr738860: a build a pre-land FIX/REVERT sent back reads only
            # a verdict NEWER than the send-back. The ``since`` bound is
            # the newest ended JOB, and none ran since — so the verdict
            # the previous build posted (the ``no-op:`` that was held for
            # review and reviewed) would be found again, re-hold the land
            # and release the reviewer onto the same head, every pass.
            sent_back = _sent_back_at(store, n, held)
            if sent_back is not None and (since is None or sent_back > since):
                since = sent_back
            # gr738860: the coordinator's answer to a stop is a newer
            # brief; a verdict from before it is the rejected head's, and
            # replaying it would restore that head and close the build
            # before dispatch runs the rebuild on the brief.
            from precis.workers.job_types import run_slice as _rs

            brief_at = getattr(
                _newest_brief_chunk(n, _rs._todo_chunks(store, n.id)),
                "created_at",
                None,
            )
            if isinstance(brief_at, datetime) and (since is None or brief_at > since):
                since = brief_at
                sent_back = max(sent_back or brief_at, brief_at)
        verdict = _verdict_comment(store, n.id, since=since)
        if (
            verdict is None
            and src is not None
            and src.status == "failed"
            and (sent_back is None or src.created_at > sent_back)
        ):
            verdict = any_verdict(_job_event_text(store, src.id))
        if verdict is None and sent_back is not None:
            older = _verdict_comment(
                store, n.id, since=bound.created_at if bound is not None else None
            )
            if older is not None:
                res.notes.append(
                    f"td{n.id}: `{older[0]}:` verdict is older than the pre-land "
                    "send-back — ignored; left open for the rebuild (gr738860)"
                )
            continue
        if verdict is None or _foreign_verdict(store, n, verdict):
            continue
        kind_, why = verdict
        if _preland_stop_open(plan, n):
            # td763175: an open pre-land stop holds the build whatever the
            # verdict says, even when the ask's blocker edge is missing —
            # read here, after the verdict, so the ignored verdict stays
            # visible in the pass notes (gr738860).
            res.notes.append(
                f"td{n.id}: `{kind_}:` ignored — held on an open "
                "coordinator ask (pre-land stop); left open (td763175)"
            )
            continue
        if n.meta.get(_templates.PRELAND_DEFER_FAILED_KEY):
            # gr663738: the stamp means the last pre-land defer failed —
            # the branch never reached origin, so a `validated:` "the
            # work was already built and committed" handoff is the stale
            # read the failed push left, not proof anything landed; a
            # `no-op:`/`moot:` close would strand the stamp the same way.
            # The node stays open for the retry the lease-guarded push
            # fixes.
            res.notes.append(
                f"td{n.id}: `{kind_}:` ignored — "
                "meta.preland_defer_failed is stamped; left open (gr663738)"
            )
            continue
        if (
            kind_ in ("validated", "no-op")
            and isinstance(held_params, dict)
            and not held_land
        ):
            if plan.open_asks(n):
                # gr738860: a pre-land stop holds this build on an open
                # coordinator ask — the verdict neither re-holds the land
                # (releasing the reviewer onto the stopped head) nor
                # closes; the answer re-arms the build.
                res.notes.append(
                    f"td{n.id}: `{kind_}:` ignored — held on an open "
                    "coordinator ask; left open (gr738860)"
                )
                continue
            reviewer_id = _dict_of(held).get("reviewer")
            reviewer_node = (
                plan.nodes.get(reviewer_id) if isinstance(reviewer_id, int) else None
            )
            if kind_ == "no-op" and reviewer_node is not None:
                denied = _denied_noop_reason(store, plan, w, n)
                if denied is not None:
                    _park_rejected_head(
                        plan,
                        w,
                        res,
                        reviewer_node,
                        n,
                        str(held_params.get("head") or ""),
                        now or datetime.now(UTC),
                        denied,
                    )
                    continue
            rejected = (
                _held_on_rejected_head(store, plan, w, reviewer_node, n, held_params)
                if reviewer_node is not None
                else ""
            )
            if rejected and reviewer_node is not None:
                # gr762396: this re-hold would release the reviewer this
                # very pass (dispatch runs after this step) onto the head
                # its last send-back rejected — park instead.
                _park_rejected_head(
                    plan,
                    w,
                    res,
                    reviewer_node,
                    n,
                    rejected,
                    now or datetime.now(UTC),
                )
                continue
            # gr681208: a pre-land FIX/REVERT reopened this build and
            # stashed its held land in ``meta.preland_held`` — the
            # rebuild's verdict is "the pushed branch is ready as it
            # stands", not "nothing to land". Re-stamp ``pending_land``
            # and close held exactly as a fresh defer does: the land
            # gate holds the dependants and releases the reviewer for
            # the re-gate. The bare close was the incident — it released
            # dependants onto a main missing the work and let the
            # dead-chain sweep won't-do the review. A ``pending_land``
            # that already stands is a fresher hold than the stash —
            # never overwritten here.
            reviewer = held.get("reviewer") if isinstance(held, dict) else None
            w.stamp_meta(
                n,
                {
                    _templates.PENDING_LAND_KEY: dict(held_params),
                    _templates.PRELAND_HELD_KEY: None,
                },
            )
            w.set_status(n, "done")
            w.comment(
                n,
                f"plan_dispatch: closed on verdict `{kind_}: {why[:300]}` "
                "— the branch is ready as it stands; land held for "
                f"review td{reviewer} (gr681208)",
            )
            res.notes.append(
                f"td{n.id}: verdict `{kind_}: {why[:200]}` — closed done, "
                f"land re-held for review td{reviewer}"
            )
            continue
        if (
            kind_ in ("validated", "no-op")
            and not held_land
            and not isinstance(held_params, dict)
            and _open_preland_reviewers(plan, n)
        ):
            # gr681208's residual hole: the same verdict on a build a
            # live pre-land review still gates, with no hold and no
            # stash to restore (a send-back from before the stash — a
            # truthy-but-unusable ``preland_held`` is no stash either,
            # not a pass through to the close) — leave it open for the
            # re-gate like the defer-failed skip.
            res.notes.append(
                f"td{n.id}: `{kind_}:` ignored — a live pre-land review "
                "gates its land and no pending_land/preland_held stands; "
                "left open (gr681208)"
            )
            continue
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


def _cool_moot_gripe(store: Any, gid: int) -> str | None:
    """Stamp the gripe's dispatch cooldown (gr759153) and return the
    ISO until, ``None`` when the setting is 0 (off). A moot the tick
    cannot prove pauses the re-mint loop — ``gripe_dispatch.candidates``
    skips the gripe while the stamp is in the future."""
    from precis.workers import gripe_dispatch as _gd

    hours = _gd.moot_cooldown_hours(store)
    if hours <= 0:
        return None
    until = (
        (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    )
    store.stamp_ref_meta(gid, {_gd.DISPATCH_COOLDOWN_META: until})
    return until


def _unverified_moot_gripe(
    store: Any, n: _Node, gid: int, mv_line: Any, res: PassResult
) -> None:
    """Preserve the verdict and cool dispatch when the build supplied no
    verified fix. An ancestor of origin/main alone proves nothing about
    the gripe. Keep both writes best-effort so the drop can finish."""
    try:
        note = _gripe_verdict_note(store, gid, n, mv_line)
        if note:
            res.notes.append(note)
    except Exception as exc:
        res.notes.append(f"td{n.id}: gripe verdict note failed ({exc})")
    try:
        until = _cool_moot_gripe(store, gid)
        if until is not None:
            res.notes.append(
                f"gripe gr{gid}: dispatch cooled until {until} (moot unproven)"
            )
    except Exception as exc:
        res.notes.append(f"td{n.id}: gripe cooldown stamp failed ({exc})")


def _moot_would(gid: int, sha: str | None, res: PassResult) -> None:
    if sha is not None:
        res.notes.append(f"would resolve gripe gr{gid} on verified moot sha {sha[:9]}")
    else:
        res.notes.append(
            f"would note the moot verdict on gr{gid} and cool its dispatch"
        )


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
    one alert on the root tells a human the plan went away.

    An unverified moot gets the ``— plan dropped`` note and a dispatch
    cooldown. Ancestry alone cannot establish that a commit fixed the gripe."""
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
                if gid is not None:
                    _moot_would(gid, sha, res)
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
                _unverified_moot_gripe(store, n, gid, mv_line, res)
            if root is not None and not _closed(root):
                w.comment(root, f"plan_dispatch: plan closed — {why}")
                w.set_status(root, "won't-do")
                closed += 1
                res.phases_closed += 1
            res.notes.append(
                f"plan td{n.root} closed as won't-do: {why}; {closed} node(s)"
            )
        if w.dry_run:
            if gid is not None:
                _moot_would(gid, sha, res)
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
        elif gid is not None and sha is None and node_kind(n.meta) == "generator":
            # The task arm already made these writes before its root close.
            _unverified_moot_gripe(store, n, gid, mv_line, res)
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
    unsettled_subtrees: set[int] = set()
    for n in sorted(plan.todos(), key=lambda x: (-x.depth, x.id)):
        kids = plan.child_todos(n)
        release_root = n.id == n.root and "release-pass" in n.open_tags
        # Propagate unfinished work through closed intermediate todos.
        # Otherwise the outer phase can close and drop the remaining
        # work from the next tick's candidate scan (gr722570).
        if any(
            c.id in unsettled_subtrees
            or isinstance(c.meta.get(_templates.PENDING_LAND_KEY), dict)
            or not (
                _closed(c)
                or _informational_ask(c)
                or (release_root and _is_ask_node(c))
            )
            for c in kids
        ):
            unsettled_subtrees.add(n.id)
        if _closed(n) or n.status == "paused":
            continue
        kind = node_kind(n.meta)
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
            if n.id in unsettled_subtrees:
                continue
            if kids:
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"{kind} td{n.id} closed: subtree settled")
            elif not kids and _analysis_posted(plan, w.store, n):
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"analyst td{n.id} closed: analysis: comment posted")
        elif kind == "task" and not kids:
            # A run_slice task closes through its land: the newest job is
            # then a succeeded land_slice (its ship closes the todo first;
            # this is the backstop). A succeeded run_slice alone never does.
            jobs = plan.jobs(n)
            # A pin-bump node closes only once its gate image carries the
            # pin (td659522 F7, Reto 2026-11-15): its record's
            # ``image.ready``, which its own image wait stamps before it
            # closes the node (this is the backstop). The land alone never
            # closes it — glue blocked by it would build on a stale image.
            if _op_type(n) == PIN_BUMP_OP:
                if _pin_image_ready(n) and not plan.live_job(n):
                    w.set_status(n, "done")
                    res.phases_closed += 1
                    res.notes.append(
                        f"task td{n.id} closed: the gate image carries the pin"
                    )
                elif (
                    jobs
                    and jobs[-1].status == "succeeded"
                    and jobs[-1].meta.get("job_type") == "land_slice"
                ):
                    res.notes.append(f"task td{n.id} held open: gate image wait (F7)")
                continue
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


def _node_repo(plan: _Plan, n: _Node) -> str:
    """The one repo ``n`` builds in: its first governing ``repo:`` tag,
    else the default repo."""
    repos = _node_repos(plan, n)
    return repos[0] if repos else _lanes.DEFAULT_REPO


def _is_generator(n: _Node) -> bool:
    """``n`` is a generator (triage, brief, reviewer, analyst): its session
    commits nothing, so it neither holds nor waits in the overlap hold."""
    return node_kind(n.meta) == "generator"


def _claims(n: _Node) -> list[str]:
    return [v for v in n.open_tags if v.startswith("claimed-by:")]


def _held_by_registry(plan: _Plan, n: _Node) -> bool:
    """Any shared doable-exclusion tag (``child-failed:`` included, unlike
    :func:`_excluded`) or ``rescue:`` on ``n`` or an ancestor."""
    held = (*_DOABLE_EXCLUSION_TAGS, "rescue:")
    for node in (n, *plan.ancestors(n)):
        for value in node.open_tags:
            for t in held:
                if (t.endswith(":") and value.startswith(t)) or value == t:
                    return True
    return False


def _node_live(plan: _Plan, n: _Node) -> bool:
    """Whether ``n`` is work in motion: a non-terminal child job or a
    ``claimed-by:`` tag, or the tick would dispatch it — the
    :func:`_step_dispatch` predicate (open task shape, no hold tag, not
    blocked or paused). A parked node (``ask-user``, ``waiting-for:*``,
    ``child-failed:*``, paused, blocked, rescue) is not live. The
    file-overlap hold does not stop queueing. :func:`live_nodes` separates
    running work from queued or undispatched work for the census."""
    if plan.live_job(n) or _claims(n):
        return True
    if n.status != "open" or not _dispatchable(plan, n):
        return False
    return not (_held_by_registry(plan, n) or plan.blocked(n) or plan.paused(n))


def live_and_held_nodes(store: Any, roots: list[int]) -> tuple[set[int], set[int]]:
    """``(running, waiting)`` todos under ``roots`` (gr702610).

    Running jobs and explicit claims consume capacity. Queued jobs and
    doable nodes remain moving for the stall sweep, including builds
    waiting on file overlap at claim time, but hold no running slot.
    """
    if not roots:
        return set(), set()
    plan = _load(store, list(roots))
    moving = {n.id for n in plan.todos() if _node_live(plan, n)}
    live = {
        i
        for i in moving
        if _claims(plan.nodes[i])
        or any(j.status == "running" for j in plan.jobs(plan.nodes[i]))
    }
    # Waiting work remains moving for stall detection, but only a running
    # job or explicit claim consumes capacity. This includes queued builds
    # held on files and doable nodes awaiting their first dispatch.
    return live, moving - live


def live_nodes(store: Any, roots: list[int], *, count_held: bool = False) -> set[int]:
    """Running todos under ``roots``; ``count_held`` includes waiting work.

    Used-slot census excludes every queued or undispatched build, whether
    held on files or immediately claimable. Stall detection includes both
    sets, because work awaiting a host is still moving.
    """
    live, held = live_and_held_nodes(store, roots)
    return live | held if count_held else live


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
    return _free_failure(j) or _voided(j)


def _free_failure(j: _Node) -> bool:
    """The job's launch was refused by its host (``meta.host_refusal``) or
    it failed outside the diff's control (``meta.attempt_free``)."""
    return bool(j.meta.get("host_refusal") or j.meta.get(ATTEMPT_FREE_KEY))


def _attempt_jobs(
    plan: _Plan, n: _Node, *, include_conflict: bool = False
) -> list[_Node]:
    """The ended jobs counting as attempts on ``n``, oldest first —
    what :func:`_attempts` counts. ``include_conflict`` keeps the conflict
    path's attempts and land reds in the list: the stamps a refusal
    leaves on a job (:func:`_human_hit`, :func:`_model_rule_hit`,
    :func:`_coordinator_hit`) are read from the newest ended job whatever
    path minted it."""
    asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
    since = max((b.created_at for b in asks), default=None)
    floor = _rearm_floor(n)
    jobs = plan.jobs(n)
    pin = _is_pin_bump(n)
    return [
        j
        for i, j in enumerate(jobs)
        # Ended only: a job still queued — never claimed, e.g. waiting for
        # a host with its placement capabilities (td543917) — is no attempt.
        if j.status in _JOB_ENDED
        and not _no_attempt(j)
        and (since is None or j.created_at > since)
        and (floor is None or j.id > floor)
        # A conflict attempt, and the land red that sent for it or its
        # resolution's land red, are the conflict path's tries, bounded by
        # :data:`CONFLICT_MAX_TRIES`, never the task's ordinary attempts.
        and (
            include_conflict
            or not (_is_conflict_job(j) or _conflict_red_land(plan, n, i, jobs))
        )
        # A build that handed off to the lander is one attempt with its
        # land_slice, not two (a pin_bump likewise, td659522 step 4).
        and not (
            j.status == "succeeded"
            and j.meta.get("job_type") in (DEFAULT_OP, PIN_BUMP_OP)
            and any(k.meta.get("job_type") == "land_slice" for k in jobs[i + 1 :])
        )
        # A pin bump's land is not its end: the node waits for the gate
        # image next (td659522 F7), so the succeeded land is no attempt
        # and the image wait is not a retry over the cap.
        and not (
            pin and j.status == "succeeded" and j.meta.get("job_type") == "land_slice"
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
    # ``attempt:<n>`` counts every red, a conflict attempt's and a
    # conflicting land's included: a task that has a land conflict — open
    # now, or any conflict attempt among its jobs, so the count stays put
    # after the episode ends — counts its ordinary job rows alone
    # (:func:`_attempt_jobs` leaves the conflict path's tries out). Reds
    # from before the episode still count; conflict-era reds never do.
    conflicted = (
        isinstance(n.meta.get(CONFLICT_KEY), dict)
        or n.meta.get(CONFLICT_TRIES_KEY) is not None
        or any(_is_conflict_job(j) for j in plan.jobs(n))
    )
    if not asks and _rearm_floor(n) is None and not conflicted:
        return max(len(ended), _attempt_tag(n))
    return len(ended)


def _human_hit(plan: _Plan, n: _Node) -> list[str]:
    """The ``lanes.HUMAN_ONLY_PATHS`` paths the newest attempt's branch
    touched, or ``[]`` — the job-meta stamp :data:`HUMAN_ONLY_KEY`
    ``run_slice`` writes (td584381 S1a). Only the newest attempt job's
    stamp counts: a stamped job below the re-arm floor or the newest ask
    was already answered for."""
    ended = _attempt_jobs(plan, n, include_conflict=True)
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
    ended = _attempt_jobs(plan, n, include_conflict=True)
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
    ended = _attempt_jobs(plan, n, include_conflict=True)
    if not ended or ended[-1].status != "failed":
        return None
    why = ended[-1].meta.get(MODEL_RULE_KEY)
    return str(why) if why else None


def _rearm_floor(n: _Node) -> int | None:
    raw = n.meta.get("rearmed_after_job")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _capacity_refused(j: _Node) -> bool:
    """A failed ``land_slice`` whose ship launch the worker host refused for
    capacity — the structured marker ``land_slice._fail`` writes
    (``attempt_free == "infra-land"`` with ``red_class ==``
    ``land_slice.ADMISSION_RED_CLASS``), never the error prose: the host
    is busy, nothing is wrong with the branch or the land."""
    from precis.workers.job_types import land_slice as _land_slice

    return (
        j.status == "failed"
        and j.meta.get("job_type") == "land_slice"
        and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
        and j.meta.get("red_class") == _land_slice.ADMISSION_RED_CLASS
    )


def _refused_at(j: _Node) -> datetime:
    """When a capacity refusal happened: ``meta.finished_at`` (the job's
    end), else its mint — a refusal's mint can precede it by the whole
    time the land queued for its slot."""
    raw = j.meta.get("finished_at")
    if isinstance(raw, str) and raw:
        try:
            at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return j.created_at
        return at if at.tzinfo else at.replace(tzinfo=UTC)
    return j.created_at


def _capacity_wait_done(plan: _Plan, n: _Node, now: datetime) -> str | None:
    """The host name when ``n``'s land has been refused for capacity
    continuously for longer than :data:`LAND_CAPACITY_WAIT_CAP`, else
    ``None``. Continuous means every job since the first refusal in the
    tail run is a capacity refusal (:func:`_capacity_refused`); any other
    job ends the run, and so does the floor ``meta.rearmed_after_job`` — an
    answered ask re-arms the wait. The run is measured to ``now``, not to
    the newest refusal, so the ask lands on time while the free-failure
    backoff is still holding the next mint. The clock starts when the first
    refusal finished (:func:`_refused_at`), not at its mint: a land queued
    for the host's slot can be old before it ever ran (queue waits past
    45 min happen). One refusal never fires: the run needs at least two. The host is the land's
    ``params.target_node`` (``"unknown"`` when the params name none)."""
    floor = _rearm_floor(n)
    run: list[_Node] = []
    for j in reversed(plan.jobs(n)):
        if floor is not None and j.id <= floor:
            break
        if not _capacity_refused(j):
            break
        run.append(j)
    if len(run) < 2 or now - _refused_at(run[-1]) <= LAND_CAPACITY_WAIT_CAP:
        return None
    params = run[0].meta.get("params")
    host = params.get("target_node") if isinstance(params, dict) else None
    return str(host) if host else "unknown"


def _infra_red_lands(plan: _Plan, n: _Node) -> int:
    """The run of consecutive failed/timed-out ``land_slice`` jobs marked
    ``attempt_free == "infra-land"`` at the tail of ``n``'s jobs, counting
    only jobs after ``meta.rearmed_after_job`` (an answered ask re-arms,
    gr570187). A land red on the diff itself — or any other job — ends the
    run: the node goes back through a build, not another land. A
    wall-deadline kill carries ``STATUS:timed-out`` instead of ``failed``
    (td648501) but is the same infra-class red.

    A land refused for capacity (:func:`_capacity_refused`) is neither
    counted nor a break in the run: the host was busy, the gate never ran,
    so nothing outside the diff went wrong that a "land again" ask could
    answer (two refusals minutes apart filed that ask on release lands
    while the host was full). One real infra red plus one capacity refusal is
    one toward :data:`LAND_AGAIN_CAP`, not two; the refusals have their
    own bound, :func:`_capacity_wait_done`."""
    floor = _rearm_floor(n)
    count = 0
    for j in reversed(plan.jobs(n)):
        if floor is not None and j.id <= floor:
            break
        if _capacity_refused(j):
            continue
        if not (
            j.status in ("failed", "timed-out")
            and j.meta.get("job_type") == "land_slice"
            and j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
        ):
            break
        count += 1
    return count


def _free_run(
    plan: _Plan, n: _Node, *, skip_capacity: bool = False
) -> tuple[str | None, int]:
    """The consecutive run of identical ``attempt_free`` reasons on
    failed/timed-out jobs at the tail of ``n``'s jobs — ``(reason,
    count)``, ``(None, 0)`` when the newest job is not a free failure.
    Floored by ``meta.rearmed_after_job`` like :func:`_infra_red_lands`:
    an answered ask re-arms the run. A different reason, a real attempt,
    a success or a non-ended job ends it (gr662077). ``skip_capacity``
    passes over a land refused for capacity (:func:`_capacity_refused`)
    without counting it: the backoff paces on the full run, but the cap
    must not read capacity refusals as a wedged holder."""
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
        if skip_capacity and _capacity_refused(j):
            continue
        if reason is None:
            reason = str(r)
        elif str(r) != reason:
            break
        count += 1
    return reason, count


def _free_done(plan: _Plan, n: _Node) -> str | None:
    """The signature that parks the node behind a coordinator ask, or
    ``None`` while the node may keep retrying (gr662077): the identical
    ``attempt_free`` reason whose consecutive run hit :data:`FREE_RUN_CAP`
    (lands refused for capacity do not count: their bound is
    :func:`_capacity_wait_done`). Every reason is treated alike, the
    lander's :data:`INDEX_LOCK_WAIT_KEY` included."""
    reason, run = _free_run(plan, n, skip_capacity=True)
    if reason is not None and run >= FREE_RUN_CAP:
        return reason
    return None


#: ``attempt_free`` reason the lander stamps verbatim when a live git holds
#: the ``index.lock`` of its own ship folder past the lander's wait
#: (gr684320). Builders no longer share a folder, so ``run_slice`` never
#: writes it; the lander does until its own handling goes.
INDEX_LOCK_WAIT_KEY = "index-lock"


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
    ``land_slice`` — the newest job is an infra-land failure or the
    lander's own ship-folder lock wait (:data:`INDEX_LOCK_WAIT_KEY`) and
    the run is under :data:`LAND_AGAIN_CAP` — else ``None``. The lock wait
    counts toward no re-land cap; the free-failure backoff paces it and
    :data:`FREE_RUN_CAP` of them in a row ask (:func:`_free_done`). The
    lander re-runs ship on the same branch; no builder is asked to fix
    what the gate's environment broke. On the first tick after an answered re-land ask
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
        and (
            j.meta.get(ATTEMPT_FREE_KEY) == "infra-land"
            # The lander's own lock wait re-lands like an infra red:
            # nothing in the diff failed, so no builder is minted.
            or j.meta.get(ATTEMPT_FREE_KEY) == INDEX_LOCK_WAIT_KEY
        )
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
                # gr738762: the raw CONFLICT lines never shadow a release
                # conflict's — main is the wrong base for it.
                if slug == "conflict" and found and found[0] == "release-conflict":
                    break
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


def _node_land_target(store: Any, n: _Node) -> str:
    """The release branch the node lands on, read as the lander reads it
    (``meta.land_target``, else a ``land target:`` brief line); the
    shape ``release/r<N>`` when it names none — a release conflict on a
    node with no recorded target still must not say main."""
    from precis.workers.job_types import run_slice

    body, latest, _latest_ord = run_slice._brief_parts(store, n.id)
    return _release.land_target_of(n.meta, body, latest) or "release/r<N>"


def _lint_red_tail(plan: _Plan, store: Any, n: _Node) -> list[tuple[int, str]]:
    """The last two consecutive lint reds, newest first.

    Lander comments are unsigned. Use the newest lander handoff in each
    attempt's time window so a red cannot inherit an older land's errors.
    """
    from precis.workers.job_types import run_slice

    attempts = _attempt_jobs(plan, n)
    if not attempts or attempts[-1].meta.get("job_type") != "land_slice":
        return []
    comments = _comments(store, n.id)
    reds: list[tuple[int, str]] = []
    before = None
    for job in reversed(attempts[-2:]):
        if job.status != "failed" or job.meta.get("job_type") != "land_slice":
            break
        handoff = next(
            (
                str(c.text)
                for c in comments
                if c.created_at >= job.created_at
                and (before is None or c.created_at < before)
                and str(c.text)
                .lstrip()
                .lower()
                .startswith(run_slice._LANDER_HANDOFF_PREFIX)
            ),
            "",
        )
        errors = handoff.partition(f"\n{_LAND_GATE_ERROR_HEADER}\n")[2].split(
            "\n\n", 1
        )[0]
        if not _LINT_ERROR_RE.search(errors):
            break
        reds.append((job.id, errors))
        before = job.created_at
    return reds


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
    if "{target}" in brief:
        brief = brief.replace("{target}", _node_land_target(store, n))
    repeats = 1
    for j in reversed(ended[:-1]):
        if repeats > MECH_RETRY_CAP or j.status != "failed":
            break
        prev = _mech_hit(_job_event_text(store, j.id))
        if prev is None or prev[0] != slug:
            break
        repeats += 1
    return slug, brief, line, repeats, int(last.id)


def _latest_builder_rung(
    plan: _Plan, n: _Node, rungs: tuple[_lanes.Rung, ...]
) -> int | None:
    """The newest builder's launched rung, falling back to its mint params."""
    for job in reversed(plan.jobs(n)):
        if str(job.meta.get("job_type") or DEFAULT_OP) != DEFAULT_OP:
            continue
        params = _dict_of(job.meta.get("params"))
        lane = job.meta.get("lane") or params.get("lane")
        model = job.meta.get("model") or params.get("model")
        return (
            _lanes.rung_index(rungs, str(lane), str(model) if model else None)
            if lane
            else None
        )
    return None


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
    idx = _latest_builder_rung(plan, n, rungs)
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


# ── the land-conflict path (gr741695, Reto's ruling gr735593) ────────
#
# A land red on a rebase or merge conflict against the base stamps the
# task's ``meta.conflict`` (the lander, :data:`CONFLICT_KEY`) and fails as
# an ordinary red; the task reopens with ``meta.build_ref`` standing, so
# the next attempt continues from the branch tip on the pinned host. The
# tick answers with a conflict-only attempt (``params.conflict``), reviews
# only its resolution (``params.resolution``), lands it ahead of everything
# else, and gives up after :data:`CONFLICT_MAX_TRIES` tries. The tick has
# no git: "answered" is read from job order, never from commits.


def _parse_ts(raw: Any) -> datetime | None:
    """An ISO timestamp (``...Z`` or offset) as an aware datetime, else
    ``None``."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _stamp_job(conflict: dict[str, Any]) -> int | None:
    """The land job id the lander recorded in ``meta.conflict.job`` (a
    positive int, not a bool), else ``None`` for a stamp that predates the
    field or names no job."""
    raw = conflict.get("job")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return None


def _stamp_after(conflict: dict[str, Any], job: _Node) -> bool:
    """The conflict stamp is newer than ``job``: its land job came after it
    (``conflict.job > job.id``). Two clocks must not decide this — the
    stamp's ``at`` is the land host's, ``created_at`` the database's — so
    the job ids do; ``at`` against ``created_at`` only for a stamp without
    ``job``."""
    by = _stamp_job(conflict)
    if by is not None:
        return by > job.id
    stamped = _parse_ts(conflict.get("at"))
    return stamped is not None and stamped > job.created_at


def _stamp_by(conflict: dict[str, Any], land: _Node) -> bool:
    """The conflict stamp was written by ``land`` or a later land
    (``conflict.job >= land.id``); for a stamp without ``job``, ``at`` is
    later than the land's mint time (``created_at``)."""
    by = _stamp_job(conflict)
    if by is not None:
        return by >= land.id
    stamped = _parse_ts(conflict.get("at"))
    return stamped is not None and stamped > land.created_at


def _is_conflict_job(j: _Node) -> bool:
    """A ``run_slice`` job minted as a conflict-only attempt: its params
    carry ``conflict`` (:func:`_conflict_params`)."""
    params = j.meta.get("params")
    return (
        str(j.meta.get("job_type") or DEFAULT_OP) == DEFAULT_OP
        and isinstance(params, dict)
        and isinstance(params.get(CONFLICT_KEY), dict)
    )


def _dict_of(raw: Any) -> dict[str, Any]:
    """``raw`` as a dict copy when it is one, else ``{}`` — a meta value a
    hand or an older writer left in another shape never crashes a pass."""
    return dict(raw) if isinstance(raw, dict) else {}


def _conflict_tries(n: _Node) -> tuple[int, int | None, int | None]:
    """``(n, job, sent_back)`` of ``meta.conflict_tries``: the newest
    conflict attempt's try number and job id and the attempt job a
    resolution review sent back; ``(0, None, None)`` for none."""
    rec = n.meta.get(CONFLICT_TRIES_KEY)
    if not isinstance(rec, dict):
        return 0, None, None

    def num(key: str) -> int | None:
        v = rec.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return num("n") or 0, num("job"), num("sent_back")


def _conflict_unusable(n: _Node) -> str | None:
    """Why the strongest rung cannot take try :data:`CONFLICT_STRONG_TRY`
    for good (``meta.conflict_tries.unusable``, stamped by the mint), else
    ``None``."""
    rec = n.meta.get(CONFLICT_TRIES_KEY)
    why = rec.get("unusable") if isinstance(rec, dict) else None
    return why if isinstance(why, str) and why else None


def _conflict_void(plan: _Plan, n: _Node) -> str | None:
    """Why ``n``'s ``meta.conflict`` no longer stands, or ``None`` while it
    does. Two cases end the episode (:func:`_step_conflict_clear` clears
    the keys and says so):

    * no ``meta.build_ref`` is left (cleared by hand): no attempt can
      continue from a tip no hub holds;
    * a ``land_slice`` newer than the newest conflict attempt has ended
      (any terminal state) and the stamp was not written by it or a later
      land (:func:`_stamp_by`: ``meta.conflict.job``, else ``at`` against
      the land's mint time ``created_at``): the land did not conflict
      again, so it failed for another reason, was cancelled or timed out.
      Everything after is the ordinary path. A newer land still live keeps
      the episode open, and so does one that failed outside the diff
      (:func:`_no_attempt`).

    The lander clears ``meta.conflict`` itself only on a successful land."""
    if not isinstance(n.meta.get(CONFLICT_KEY), dict):
        return None
    if not isinstance(n.meta.get(_templates.BUILD_REF_KEY), dict):
        return "the task holds no build_ref any more — no attempt can continue"
    _k, job_id, _sent = _conflict_tries(n)
    if job_id is None:
        return None
    lands = [
        j
        for j in plan.jobs(n)
        if j.id > job_id
        and j.meta.get("job_type") == "land_slice"
        # A land that failed outside the diff (attempt_free: the host was
        # busy, the gate's machinery died) says nothing about the branch:
        # the episode stands and the re-land path mints again.
        and not _no_attempt(j)
    ]
    if not lands or any(j.status not in _JOB_TERMINAL for j in lands):
        return None
    last = max(lands, key=lambda j: j.id)
    if not _stamp_by(n.meta[CONFLICT_KEY], last):
        return (
            f"land jo{last.id} ended ({last.status}) without a new conflict "
            "stamp — the land failed for another reason"
        )
    return None


def _conflict_view(plan: _Plan, n: _Node) -> tuple[str, int, _Node | None]:
    """``(state, try, attempt job)`` of ``n``'s land conflict.

    ``state`` is ``"none"`` (no ``meta.conflict``, or a void one,
    :func:`_conflict_void`), ``"try"`` (conflict attempt number ``try`` is
    due), ``"wait"`` (the newest attempt is live, or its pushed work is on
    its way through review and land) or ``"spent"``
    (:data:`CONFLICT_MAX_TRIES` tries failed, or the strongest rung cannot
    be used, :func:`_conflict_unusable`). The attempt job is the one
    ``meta.conflict_tries.job`` names.

    A try has failed when its job ended without pushed work
    (``meta.push.state != "pushed"``), when the lander stamped the conflict
    again after the job was minted (:func:`_stamp_after`: the land conflicted
    again), or when
    its resolution review sent it back (``conflict_tries.sent_back``). A
    cancelled job, one the seat voided and one that failed outside the
    diff (``attempt_free``, ``host_refusal``) burned nothing — the same
    "not an attempt" test the ordinary count uses (:func:`_no_attempt`):
    the same try mints again."""
    conflict = n.meta.get(CONFLICT_KEY)
    if not isinstance(conflict, dict) or _conflict_void(plan, n) is not None:
        return "none", 0, None
    k, job_id, sent_back = _conflict_tries(n)
    if _conflict_unusable(n) is not None:
        return "spent", k, None
    after = ("spent", k) if k >= CONFLICT_MAX_TRIES else ("try", k + 1)
    if k <= 0 or job_id is None:
        return "try", 1, None
    job = next((j for j in plan.jobs(n) if j.id == job_id), None)
    if job is None:
        # Minted by this pass: the loaded plan does not hold it yet.
        return "wait", k, None
    if job.status not in _JOB_TERMINAL:
        return "wait", k, job
    if (
        job.status == "cancelled"
        or _voided(job)
        or (job.status != "succeeded" and _no_attempt(job))
    ):
        return "try", k, job
    push = job.meta.get("push")
    pushed = isinstance(push, dict) and push.get("state") == "pushed"
    if (
        job.status != "succeeded"
        or not pushed
        or sent_back == job.id
        or _stamp_after(conflict, job)
    ):
        return *after, job
    return "wait", k, job


def _conflict_state(plan: _Plan, n: _Node) -> tuple[str, int]:
    """:func:`_conflict_view` without the job."""
    state, k, _job = _conflict_view(plan, n)
    return state, k


def _conflict_done(plan: _Plan, n: _Node) -> list[str] | None:
    """The conflicted files of ``n`` when its conflict tries are spent —
    what :func:`_capped` turns into the coordinator ask — else ``None``."""
    state, _k, _job = _conflict_view(plan, n)
    if state != "spent":
        return None
    conflict = n.meta[CONFLICT_KEY]
    files = conflict.get("files")
    return [str(f) for f in files] if isinstance(files, list) else []


def _conflict_params(n: _Node, k: int) -> dict[str, Any]:
    """The ``params.conflict`` of try ``k``: ``{head, ref, base_tip, files,
    try}`` — the lander's stamp, and the try number (from 1)."""
    conflict = n.meta[CONFLICT_KEY]
    files = conflict.get("files")

    def text(key: str) -> str | None:
        v = conflict.get(key)
        return v if isinstance(v, str) and v else None

    return {
        "head": text("head"),
        "ref": text("ref"),
        "base_tip": text("base_tip"),
        "files": [str(f) for f in files] if isinstance(files, list) else [],
        "try": k,
    }


def _conflict_red_land(plan: _Plan, n: _Node, i: int, jobs: list[_Node]) -> bool:
    """``jobs[i]`` (``n``'s jobs, oldest first) is a failed ``land_slice``
    that conflicted: a conflict attempt was minted right after it, or it is
    the newest job and the lander stamped ``meta.conflict`` during its run.
    Such a red is the conflict path's, not one of the task's ordinary
    attempts."""
    j = jobs[i]
    if j.meta.get("job_type") != "land_slice" or j.status not in (
        "failed",
        "timed-out",
    ):
        return False
    if i + 1 < len(jobs):
        return _is_conflict_job(jobs[i + 1])
    conflict = n.meta.get(CONFLICT_KEY)
    if not isinstance(conflict, dict):
        return False
    by = _stamp_job(conflict)
    if by is not None:
        return by == j.id
    stamped = _parse_ts(conflict.get("at"))
    return stamped is not None and stamped >= j.created_at


def _conflict_pending_since(plan: _Plan, n: _Node) -> datetime | None:
    """When ``n``'s conflict resolution was pushed, while its land is still
    pending — held for the resolution review, queued or running, or about
    to be minted (the attempt is the task's newest job) — else ``None``.
    The push time is the attempt's ``meta.push.at``, else its mint."""
    state, _k, job = _conflict_view(plan, n)
    if (
        state != "wait"
        or job is None
        or job.status != "succeeded"
        or n.status in _RULING_STATUSES
    ):
        return None
    # A land that failed outside the diff (:func:`_no_attempt`) is a land
    # still to come, not one that ended the episode.
    newer = [
        j
        for j in plan.jobs(n)
        if j.id > job.id
        and not (j.meta.get("job_type") == "land_slice" and _no_attempt(j))
    ]
    # A held build is ``done`` with ``pending_land``; a ``done`` task with
    # neither a held nor a running land is the landed one.
    held = isinstance(n.meta.get(_templates.PENDING_LAND_KEY), dict)
    landing = any(
        j.meta.get("job_type") == "land_slice" and j.status not in _JOB_TERMINAL
        for j in newer
    )
    about_to_land = not newer and n.status != "done"
    if not (held or landing or about_to_land):
        return None
    push = job.meta.get("push")
    pushed_at = _parse_ts(push.get("at")) if isinstance(push, dict) else None
    return pushed_at or job.created_at


def _conflict_holder(
    plan: _Plan, store: Any, t: _Node, now: datetime
) -> tuple[int | None, list[int]]:
    """``(task id, lapsed ids)``: the task whose pending conflict
    resolution holds ``t``'s land — same repository, same land target,
    pushed less than :data:`CONFLICT_LAND_HOLD` ago — and the holders whose
    hold has lapsed. Two pending resolutions never hold each other both
    ways: ``t`` waits only for one pushed before its own. ``(None, …)``
    when nothing holds it."""
    mine = _conflict_pending_since(plan, t)
    holders: list[tuple[datetime, int]] = []
    lapsed: list[int] = []
    for h in plan.todos():
        if h.id == t.id:
            continue
        since = _conflict_pending_since(plan, h)
        if since is None:
            continue
        if now - since >= CONFLICT_LAND_HOLD:
            lapsed.append(h.id)
            continue
        if mine is not None and (since, h.id) > (mine, t.id):
            continue
        holders.append((since, h.id))
    if not holders:
        return None, lapsed
    inputs = _overlap_inputs(store, [t.id, *(h for _s, h in holders)])
    default: tuple[frozenset[str], str] = (frozenset(), "main")
    repo, target = _node_repo(plan, t), inputs.get(t.id, default)[1]
    for _since, hid in sorted(holders):
        held = plan.nodes.get(hid)
        if (
            held is not None
            and _node_repo(plan, held) == repo
            and inputs.get(hid, default)[1] == target
        ):
            return hid, lapsed
    return None, lapsed


def _conflict_land_note(
    plan: _Plan, store: Any, res: PassResult, t: _Node, now: datetime
) -> int | None:
    """:func:`_conflict_holder` plus its notes: the task ``t``'s land waits
    for, or ``None``. A lapsed hold is noted once per holder per pass, not
    once per land it no longer stops."""
    holder, lapsed = _conflict_holder(plan, store, t, now)
    for h in lapsed:
        note = (
            f"the land hold of td{h}'s conflict resolution lapsed after "
            f"{int(CONFLICT_LAND_HOLD.total_seconds() // 60)} min — other "
            "lands flow again"
        )
        if note not in res.notes:
            res.notes.append(note)
    if holder is not None:
        res.notes.append(
            f"td{t.id}: land held — waits for td{holder}'s conflict "
            "resolution to land first (same repository and base)"
        )
    return holder


def _resolution_param(plan: _Plan, n: _Node) -> dict[str, Any] | None:
    """``{of, ref}`` for the pre-land reviewer ``n`` of a conflict
    resolution — ``of`` the commit that failed to land (``meta.conflict.
    head``, already reviewed), ``ref`` the hub ref that holds it — or
    ``None``: a reviewer outside a pre-land flow, or one whose targets have
    no resolution held, gets none."""
    if not _is_reviewer(n) or n.meta.get(_templates.PRELAND_REVIEW_KEY) is not True:
        return None
    for t in _pending_targets(plan, n):
        state, _k, job = _conflict_view(plan, t)
        if state == "wait" and job is not None and job.status == "succeeded":
            conflict = t.meta[CONFLICT_KEY]
            return {"of": conflict.get("head"), "ref": conflict.get("ref")}
    return None


def _conflict_first(plan: _Plan, n: _Node, job_type: str | None) -> bool:
    """The job about to mint for ``n`` belongs to a pending conflict
    resolution (:func:`_conflict_pending_since`): ``n``'s own land, or a
    reviewer one of whose held targets (:func:`_pending_targets`, the set
    :func:`_resolution_param` reads too) has one."""
    if job_type == "land_slice":
        return _conflict_pending_since(plan, n) is not None
    if _is_reviewer(n):
        return any(
            _conflict_pending_since(plan, t) is not None
            for t in _pending_targets(plan, n)
        )
    return False


def _conflict_pin(
    plan: _Plan,
    w: _Writer,
    n: _Node,
    params: dict[str, Any],
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None,
    k: int,
) -> tuple[bool, str | None, str | None]:
    """Pin conflict attempt ``k`` to its rung; ``(go, note, unusable)``.
    Tries below :data:`CONFLICT_STRONG_TRY` run where the task's build ran
    (:func:`_mech_pin`); that pin cannot be made (the op pins a lane or
    model already, the rung left the ladder, its lane is backed off) and
    the attempt mints unpinned: a conflict try never climbs the ladder on
    its own. Try :data:`CONFLICT_STRONG_TRY` takes the ladder's top rung.
    It waits (``go=False``) while that lane is backed off — that clears —
    but when the strongest rung cannot be used for a reason waiting does
    not clear (the ladder has no enabled rung, its ``rung_caps`` cap is
    spent) ``unusable`` says why: the tries are spent and the conflict ask
    names it. A node whose op pins a ``model`` keeps it."""
    rungs = _task_rungs(lane_table, n, params)
    if k < CONFLICT_STRONG_TRY:
        if _mech_pin(plan, w, n, rungs, params, rules):
            return True, None, None
        return (
            True,
            f"td{n.id}: conflict try {k} mints unpinned — the build's rung "
            "could not be pinned; it does not climb the ladder",
            None,
        )
    if "model" in params:
        return (
            True,
            f"td{n.id}: conflict try {k} keeps the op's own model pin — "
            "no stronger rung to take",
            None,
        )
    if not rungs:
        why = "no enabled lane offers a rung for the task's class"
        return False, f"td{n.id}: conflict try {k} cannot run — {why}", why
    top = rungs[-1]
    if top.lane in set(_lanes.lane_backoffs(w.store)):
        return (
            False,
            f"td{n.id}: conflict try {k} waits — the strongest rung's lane "
            f"{top.lane} is backed off",
            None,
        )
    cap_usd = _rung_cap_usd(top, rules) if rules else None
    if cap_usd is not None:
        spend = _node_spend_usd(plan, n, rules or {})
        if spend >= cap_usd:
            why = (
                f"the strongest rung (lane {top.lane}, model {top.model}) is "
                f"spent: ${spend:.2f} recorded against its ${cap_usd:.2f} cap"
            )
            return False, f"td{n.id}: conflict try {k} cannot run — {why}", why
        params["budget_usd"] = round(cap_usd - spend, 4)
    params["lane"] = top.lane
    if top.model is not None:
        params["model"] = top.model
    model_bit = f" model {top.model}" if top.model else ""
    return (
        True,
        f"td{n.id}: conflict try {k} pinned to the strongest model "
        f"(lane {top.lane}{model_bit}, rung {len(rungs)}/{len(rungs)})",
        None,
    )


def _step_conflict_clear(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """Drop the conflict path's keys when they no longer stand — in one
    place, with a note. A void conflict (:func:`_conflict_void`: the task
    holds no ``build_ref`` any more, or a land newer than the newest
    attempt ended without a new stamp) loses ``meta.conflict`` and
    ``meta.conflict_tries`` and the task takes the ordinary path. A land
    that cleared ``meta.conflict`` itself (with ``meta.build_ref``) leaves
    the count behind: the tick that sees it drops it, so the next conflict
    of the task starts again at try 1."""
    for n in plan.todos():
        void = _conflict_void(plan, n)
        if void is not None:
            w.stamp_meta(n, {CONFLICT_KEY: None, CONFLICT_TRIES_KEY: None})
            res.notes.append(
                f"td{n.id}: conflict episode over — {void}; meta.conflict and "
                "meta.conflict_tries cleared, the ordinary path takes over"
            )
        elif n.meta.get(CONFLICT_TRIES_KEY) is not None and not isinstance(
            n.meta.get(CONFLICT_KEY), dict
        ):
            w.stamp_meta(n, {CONFLICT_TRIES_KEY: None})


def _step_conflict_first(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """Rule 4a: the reviewer and land jobs of a pending conflict
    resolution that are still queued — minted by ``run_slice`` or by an
    earlier pass — are raised to :data:`CONFLICT_FIRST_SCORE`; jobs the tick
    mints take it at mint (:func:`_claim_score`)."""
    for n in plan.todos():
        if _conflict_pending_since(plan, n) is None:
            continue
        jobs = [j for j in plan.jobs(n) if j.meta.get("job_type") == "land_slice"] + [
            j for r in _open_preland_reviewers(plan, n) for j in plan.jobs(r)
        ]
        for j in jobs:
            if j.status != "queued":
                continue
            have = _stamped_score(j)
            if have is not None and have >= CONFLICT_FIRST_SCORE:
                continue
            w.stamp_meta(j, {_dscore.SCORE_META_KEY: CONFLICT_FIRST_SCORE})
            res.notes.append(
                f"td{n.id}: job jo{j.id} raised to the conflict-resolution "
                f"claim score {CONFLICT_FIRST_SCORE:g}"
            )


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
    plan: _Plan,
    n: _Node,
    rungs: tuple[_lanes.Rung, ...],
    attempts: int,
    *,
    previous_rung: int | None = None,
) -> int:
    """The ladder index the next attempt climbs from — one rung above
    where the newest REAL attempt ran (a no-attempt job is no rung to
    climb from, gr571395), else the attempt count, floored at the top.
    The ``_escalate`` floor, factored so :func:`_spend_hit` reads the
    same tail the climb does."""
    if previous_rung is None:
        ended = [
            j
            for j in plan.jobs(n)
            if j.status in _JOB_ENDED and not _no_attempt(j) and not _is_conflict_job(j)
        ]
        pick = ended[-1].meta if ended else {}
        # A validator can decline a rung before stamping a launched session.
        rung = _dict_of(pick.get("diversity_retry")) or pick
        previous_rung = _lanes.rung_index(rungs, rung.get("lane"), rung.get("model"))
    return min(
        previous_rung + 1 if previous_rung is not None else attempts, len(rungs) - 1
    )


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


def _pinned_rung(
    lane_table: tuple[_lanes.Lane, ...], n: _Node, params: dict[str, Any]
) -> _lanes.Rung | None:
    """The enabled rung a model-pinned op names: the node's task-class
    ladder entry whose model is ``params["model"]`` (on ``params["lane"]``
    when that is pinned too; the first such rung in ladder order
    otherwise). ``None`` for an unpinned node or a model on no enabled
    rung — the pin then behaves as it always did, bound by nothing."""
    model = params.get("model")
    if not model:
        return None
    task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
    lane = params.get("lane")
    return next(
        (
            r
            for r in _lanes.ladder(task_class, lanes=lane_table)
            if r.model == model and (not lane or r.lane == lane)
        ),
        None,
    )


def _spend_rungs(
    lane_table: tuple[_lanes.Lane, ...], n: _Node, params: dict[str, Any]
) -> tuple[_lanes.Rung, ...]:
    """The rungs :func:`_spend_hit` weighs: just the pinned rung for a
    model-pinned node (gr739266 — the whole ladder always held an uncapped
    rung, so a capped pin never read as spent), else :func:`_task_rungs`.
    Kept apart from ``_task_rungs`` because ``_capped`` also reads that
    ladder for the no-commit top-rung rule, which a pin must not change."""
    pinned = _pinned_rung(lane_table, n, params)
    return (pinned,) if pinned is not None else _task_rungs(lane_table, n, params)


def _pinned_spend(
    w: _Writer,
    plan: _Plan,
    n: _Node,
    params: dict[str, Any],
    lane_table: tuple[_lanes.Lane, ...],
    rules: dict[str, dict[str, Any]] | None,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """:func:`_escalate`'s answer for a model-pinned node (gr739266): the
    pin is never climbed, but the pinned rung's ``rung_caps`` cap still
    binds it. An uncapped (or unknown) pin is left alone. A capped one
    stamps ``meta.spend`` and mints with ``params.budget_usd`` = the
    remaining allowance, the bound ``run_slice`` enforces mid-run
    (td601015); at or over the cap nothing mints — ``blocked: True`` is
    stamped and :func:`_spend_hit`'s caller caps the node the way a spent
    ladder node is."""
    pinned = _pinned_rung(lane_table, n, params)
    cap_usd = _rung_cap_usd(pinned, rules) if pinned is not None and rules else None
    if pinned is None or rules is None or cap_usd is None:
        return True, None, None
    spend = _node_spend_usd(plan, n, rules)
    blocked = spend >= cap_usd
    w.stamp_meta(
        n,
        {
            SPEND_KEY: {
                "usd": round(spend, 4),
                "cap_usd": cap_usd,
                "blocked": blocked,
                "at": _iso(datetime.now(UTC)),
            }
        },
    )
    if blocked:
        return (
            False,
            f"td{n.id}: pinned rung lane {pinned.lane} model {pinned.model} is "
            f"spent — ${spend:.2f} recorded against its ${cap_usd:.2f} cap; "
            "no job minted",
            None,
        )
    params["budget_usd"] = round(cap_usd - spend, 4)
    return True, None, None


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
    :data:`DEFAULT_RETRIES`, the no-lane case that still mints the ask.
    A review that exited without a readable verdict gets a second try
    even on a single-rung lane (gr681634)."""
    if n.meta.get("retries") is not None:
        return _retries(n)
    cap = len(rungs) or DEFAULT_RETRIES
    review = n.meta.get("review")
    if _is_reviewer(n) and isinstance(review, dict) and review.get("verdict") is None:
        return max(cap, 2)
    return cap


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
    now: datetime,
) -> bool:
    if _pin_awaits_land(plan, n):
        return False  # the land owns the node (td659522 step 4)
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
    # A stamped refusal caps immediately. Submit checked the reachable
    # launch and retry routes before stamping it (gr657521, gr739375).
    if _model_rule_hit(plan, n) is not None:
        return True
    if _land_again_capped(plan, n):
        return True
    # A land refused for capacity past its wait cap asks instead of
    # waiting on: the host stayed full too long to be a blip.
    if _capacity_wait_done(plan, n, now) is not None:
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
    # A land conflict has its own bound (gr741695): three resolution
    # attempts spent asks; a try still due is never held back by the
    # task's ordinary attempt count — conflict tries burn none.
    conflict = _conflict_state(plan, n)[0]
    if conflict == "spent":
        return True
    if conflict == "try":
        return False
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


def _is_repo_release(n: _Node) -> bool:
    """``n`` is a second-repo release node: its resolved mechanical op
    is :data:`REPO_RELEASE_OP` (td659522 step 3)."""
    return _resolve_op(n)[0] == REPO_RELEASE_OP


def _is_pin_bump(n: _Node) -> bool:
    """``n`` is a pin-bump node: its resolved mechanical op is
    :data:`PIN_BUMP_OP` (td659522 step 4)."""
    return _resolve_op(n)[0] == PIN_BUMP_OP


def _pin_image(n: _Node) -> dict[str, Any]:
    """The ``image`` stamp of a pin-bump node's record (``{}`` for none):
    what its gate image wait last saw (td659522 F7)."""
    record = n.meta.get(PIN_RECORD_KEY)
    image = record.get("image") if isinstance(record, dict) else None
    return image if isinstance(image, dict) else {}


def _pin_image_ready(n: _Node) -> bool:
    """The pin node's gate image carries the pin: ``image.ready`` on its
    record, stamped by the image wait before it closes the node."""
    return _pin_image(n).get("ready") is True


def _pin_land_host(plan: _Plan, n: _Node) -> str | None:
    """The ``params.target_node`` of the pin node's newest ``land_slice``
    when that land succeeded (the host whose gate image the next job
    checks; a failed image wait after it does not lose it). Without one —
    a no-op mints no land — the host a ``landed`` record's image wait
    stamped (``image.host``), so a re-dispatched wait checks the same
    image. Else ``None``."""
    lands = [j for j in plan.jobs(n) if j.meta.get("job_type") == "land_slice"]
    host: Any = None
    if lands and lands[-1].status == "succeeded":
        params = lands[-1].meta.get("params")
        host = params.get("target_node") if isinstance(params, dict) else None
    if not (isinstance(host, str) and host.strip()):
        record = n.meta.get(PIN_RECORD_KEY)
        if isinstance(record, dict) and record.get("state") == "landed":
            host = _pin_image(n).get("host")
    return host if isinstance(host, str) and host.strip() else None


def _pin_awaits_land(plan: _Plan, n: _Node) -> bool:
    """``n`` is a pin-bump node whose newest job is a succeeded
    ``pin_bump`` above the re-arm floor: the branch is handed to the
    lander, and the node waits for that land (it closes through it, and a
    failed land re-dispatches the build). Neither a cap ask nor a second
    ``pin_bump`` may fire in this state."""
    if not _is_pin_bump(n):
        return False
    jobs = plan.jobs(n)
    if not jobs:
        return False
    j = jobs[-1]
    floor = _rearm_floor(n)
    return (
        j.status == "succeeded"
        and j.meta.get("job_type") == PIN_BUMP_OP
        and (floor is None or j.id > floor)
    )


def _pin_releases(plan: _Plan, n: _Node) -> list[_Node]:
    """The non-ask ``blocked-by`` targets of ``n`` that are ``repo_release``
    nodes inside the loaded plan."""
    return [
        m
        for b in plan.blockers.get(n.id, [])
        if not b.is_ask
        and (m := plan.nodes.get(b.id)) is not None
        and m.kind == "todo"
        and _is_repo_release(m)
    ]


def _pin_params(
    plan: _Plan, n: _Node, params: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The ``pin_bump`` params of ``n`` (``todo_id``, ``release_todo``,
    ``pin_repo``; ones already on the op are kept) and ``None``, or the
    reason it must not dispatch. ``release_todo`` is the single non-ask
    ``blocked-by`` target of ``n`` that is a ``repo_release`` node, open
    or done; blockers are read from ``plan.nodes``, so a target outside
    the loaded subtrees (no op to read) does not count. ``pin_repo`` is
    the one non-default repo governing that release node. The pin-bump
    node itself builds in precis: it must resolve to no repo or the
    default one."""
    own = _node_repos(plan, n)
    if own and own != [_lanes.DEFAULT_REPO]:
        return params, (
            f"resolves to repo {', '.join(own)}; it builds in precis "
            "(carry repo:precis or inherit no repo: tag)"
        )
    releases = _pin_releases(plan, n)
    ids = sorted({m.id for m in releases})
    if len(ids) != 1:
        return params, (
            f"needs exactly one {REPO_RELEASE_OP} blocked-by target, found {len(ids)}"
        )
    repos = _node_repos(plan, releases[0])
    if len(repos) != 1 or repos[0] == _lanes.DEFAULT_REPO:
        return params, (
            f"release td{ids[0]} must govern exactly one non-default repo, "
            f"found {repos or 'none'}"
        )
    out = dict(params)
    out.setdefault("todo_id", n.id)
    out.setdefault("release_todo", ids[0])
    out.setdefault("pin_repo", repos[0])
    return out, None


def _second_repo(plan: _Plan, reviewer: _Node) -> str | None:
    """The one non-default repo a reviewer's verdict concerns (td659522
    step 5): the repo governing the reviewer, else the first review target
    that has one. ``None`` for the default repo, no repo, or more than one."""
    repos = _node_repos(plan, reviewer)
    for t in () if repos else _review_targets(plan, reviewer):
        repos = _node_repos(plan, t)
        if repos:
            break
    if len(repos) != 1 or repos[0] == _lanes.DEFAULT_REPO:
        return None
    return repos[0]


def _repo_base(repo: str) -> str:
    """The base branch of ``repo``'s build profile (``main`` without one)."""
    profile = _lanes.repo_profile(repo)
    return profile.base if profile is not None else "main"


def _tag_minted_repo(
    plan: _Plan, w: _Writer, reviewer: _Node, minted_id: int, now: datetime
) -> None:
    """Carry the reviewed slice's second repo onto the fix or revert todo
    ``_mint_task`` just made under the plan root (it only has the
    ``plan:`` tag, so it would resolve to the default repo): the repo
    governing the reviewer, else the first review target that has one. Only
    a single non-default repo not already governing the minted node is
    tagged."""
    minted = plan.nodes.get(minted_id)
    repo = _second_repo(plan, reviewer)
    if minted is None or repo is None:
        return
    if _node_repos(plan, minted) == [repo]:
        return
    w.add_tag(minted, f"repo:{repo}", now)


def _gate_release_on(
    store: Any, plan: _Plan, w: _Writer, reviewer: _Node, gate_id: int, now: datetime
) -> None:
    """Hold every OPEN ``repo_release`` node of ``reviewer``'s plan behind
    ``gate_id`` too — the fix or revert todo the reviewer's verdict just
    minted — when it is ``blocked-by`` the reviewer, or governs the same
    repo(s) as the minted todo (the reviewer's when the minted one
    resolves none): a reviewer of a fix has no edge from the release. The
    reviewer closes ``done`` at FIX/REVERT, so without this edge the
    release would unblock and cut unfixed code. Idempotent: an existing
    edge is kept."""
    minted = plan.nodes.get(gate_id)
    repos = sorted(_node_repos(plan, minted)) if minted is not None else []
    repos = repos or sorted(_node_repos(plan, reviewer))
    for m in plan.todos():
        if (
            m.id == gate_id
            or m.root != reviewer.root
            or _closed(m)
            or not _is_repo_release(m)
        ):
            continue
        edges = plan.blockers.setdefault(m.id, [])
        if not any(b.id == reviewer.id for b in edges) and not (
            repos and sorted(_node_repos(plan, m)) == repos
        ):
            continue
        if any(b.id == gate_id for b in edges):
            continue
        if not w.dry_run:
            store.add_link(src_ref_id=m.id, dst_ref_id=gate_id, relation="blocked-by")
        edges.append(_Blocker(id=gate_id, status="open", is_ask=False, created_at=now))


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
    *,
    from_builder: bool = False,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Pin a retried ``run_slice`` one rung up the cross-lane ladder.

    Reto's ruling (td559457, 2026-11-03): when a slice fails, go up the
    chain before asking — free vendor lanes first, then the Claude
    subscription's models. The chain is :func:`precis.plans.lanes.ladder`
    for the node's task class (every enabled fit lane in table order, its
    models from the default upward). The retry climbs one rung above where
    the newest ended attempt ran (its job's ``meta.lane``/``meta.model``);
    when that is unknown, attempt *k* takes rung *k* from the foot. Floored
    at the top rung. A node whose ``meta.op`` already pins ``model`` is never
    climbed (gr549046: the triage's pick wins) but its rung's ``rung_caps``
    cap still binds it (:func:`_pinned_spend`, gr739266); one that pins
    ``lane`` climbs within that lane only. Rungs on lanes a host refusal backed off are
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
        return _pinned_spend(w, plan, n, params, lane_table, rules)
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
    # A lint retry ran on the same rung, which the failed land job does
    # not record. Climb from its builder's rung when it is known.
    previous_rung = _latest_builder_rung(plan, n, rungs) if from_builder else None
    start = max(
        _climb_start(plan, n, rungs, attempts, previous_rung=previous_rung),
        min(floor, len(rungs) - 1),
    )
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

#: Job types that read a task's work out of a build host's hub (gr741695):
#: the next build, a reviewer's build-seat job and the land.
_HUB_JOB_TYPES = (DEFAULT_OP, "land_slice")

#: Job meta recording a hub pin, ``{host, todo}``: the host the job is
#: pinned to and the task whose ``meta.build_ref`` holds the work there.
#: :func:`_step_unplaced` reads it to say how to release the pin.
HUB_PIN_KEY = "hub_pin"


def _build_ref_host(meta: dict[str, Any]) -> str | None:
    """The host a task's ``meta.build_ref`` names (the record the worker
    stamps after a job pushed), else ``None``."""
    rec = meta.get(_templates.BUILD_REF_KEY)
    host = rec.get("host") if isinstance(rec, dict) else None
    return host.strip() if isinstance(host, str) and host.strip() else None


def _held_land_host(meta: dict[str, Any]) -> str | None:
    """The ``target_node`` of a task's ``meta.pending_land`` (the land
    params stamped when the land is held for review), else ``None``."""
    rec = meta.get(_templates.PENDING_LAND_KEY)
    host = rec.get("target_node") if isinstance(rec, dict) else None
    return host.strip() if isinstance(host, str) and host.strip() else None


def _review_target_metas(
    plan: _Plan, store: Any, n: _Node
) -> list[tuple[int, dict[str, Any]]]:
    """``(id, meta)`` of the todos reviewer ``n`` judges, oldest id first:
    the loaded targets (:func:`_review_targets`), else the ``from`` todo
    read from the store when it lies outside the loaded plan. Empty for a
    sha-keyed reviewer."""
    targets = _review_targets(plan, n)
    if targets:
        return sorted((t.id, t.meta) for t in targets)
    fid = _review_from(n)
    ref = store.get_ref(kind="todo", id=fid) if fid is not None else None
    return [(fid, dict(ref.meta or {}))] if fid is not None and ref is not None else []


def _hub_pin(
    plan: _Plan, store: Any, n: _Node, job_type: str
) -> tuple[str, int, str] | None:
    """``(host, holder todo id, note)`` the job about to mint for ``n``
    must run on, or ``None`` when no hub holds work for it (gr741695).

    A hub is local to its host, so a task with ``meta.build_ref`` runs
    its next build, its review and its land there: a build and a land
    take the task's own record (a land also falls back to its held
    ``pending_land.target_node``); a reviewer's ``run_slice`` takes its
    review targets' (``build_ref.host``, else ``pending_land.target_node``).
    Targets on several hosts pin to the first by id, and the note says
    so; the note is empty otherwise."""
    if job_type not in _HUB_JOB_TYPES:
        return None
    if job_type == "land_slice" or not _is_reviewer(n):
        host = _build_ref_host(n.meta)
        if host is None and job_type == "land_slice":
            host = _held_land_host(n.meta)
        return (host, n.id, "") if host else None
    hosts: list[tuple[str, int]] = []
    for tid, meta in _review_target_metas(plan, store, n):
        host = _build_ref_host(meta) or _held_land_host(meta)
        if host and host not in (h for h, _ in hosts):
            hosts.append((host, tid))
    if not hosts:
        return None
    note = (
        f"review targets sit on several hosts ({', '.join(h for h, _ in hosts)}); "
        f"pinned to {hosts[0][0]}"
        if len(hosts) > 1
        else ""
    )
    return hosts[0][0], hosts[0][1], note


def _pin_job_host(params: dict[str, Any], host: str, holder: int) -> str | None:
    """Set ``params.target_node`` to the hub pin ``host``. An explicit
    ``target_node`` that names another host loses to the pin — the hub
    that holds td``holder``'s work is on ``host`` and nowhere else — and
    the returned note says so; ``None`` when nothing was overridden."""
    current = params.get("target_node")
    params["target_node"] = host
    if current and str(current) != host:
        return (
            f"params.target_node {str(current)!r} replaced by the hub pin "
            f"{host!r} (td{holder}'s work is in that host's hub)"
        )
    return None


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
        job_id, reused = mint_job_once(
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
        if not reused:
            # gr657310: a reused mint is silent — the idem key's live job
            # was already counted at its own mint.
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


def _claim_score(plan: _Plan, n: _Node, job_type: str | None = None) -> float:
    """The score stamped on ``n``'s job for the executor's claim order.

    A finished build waiting for its pre-land review or its land holds
    the files it touched, so both are claimed ahead of fresh builds: a
    pre-land reviewer and any ``land_slice`` job (``job_type``) floor at
    :data:`~precis.utils.dispatch_score.FINISH_FIRST_SCORE`. Everything
    else keeps :func:`_effective_score`."""
    score = _effective_score(plan, n)
    if _conflict_first(plan, n, job_type):
        return max(score, CONFLICT_FIRST_SCORE)
    if job_type == "land_slice" or (
        _is_reviewer(n) and n.meta.get(_templates.PRELAND_REVIEW_KEY) is True
    ):
        return max(score, _dscore.FINISH_FIRST_SCORE)
    return score


_DEVIN_EMPTY_RESPONSE_KEY = "Devin agent error: retryable empty_response"


def _review_lane_tables(
    lane_table: tuple[_lanes.Lane, ...], vendors: tuple[str, ...]
) -> Iterator[tuple[frozenset[str], tuple[_lanes.Lane, ...]]]:
    """Yield distinct vendor exclusions in review preference order."""
    exclusions = dict.fromkeys(
        (frozenset(vendors), frozenset(vendors[:1]), frozenset())
    )
    for excluded in exclusions:
        yield excluded, tuple(ln for ln in lane_table if ln.vendor not in excluded)


def _review_fallback_lane(
    store: Any,
    cls: str,
    lane_table: tuple[_lanes.Lane, ...],
    vendors: tuple[str, ...],
    chosen: str,
    backoff: dict[str, datetime],
) -> str | None:
    """The lane a review moves to when its chosen lane closes a window
    (gr761723), or ``None``: :func:`~precis.plans.lanes.pick_lane` over
    the lanes of a vendor that built none of the reviewed work — never
    the full-table step, so the move never hands the review to a
    builder's vendor — and never a metered lane (a closed subscription
    window must not buy paid compute). Capacity-blind: a full fallback
    is a slot wait, still sooner than the chosen lane's reset."""
    rest = tuple(
        ln
        for ln in lane_table
        if ln.name != chosen
        and ln.vendor not in vendors
        and ln.cost_class != _lanes.COST_METERED
    )
    alt = _lanes.pick_lane(store, cls, lanes=rest, counts={}, backoff=backoff)
    return alt.name if alt is not None else None


def _review_lane(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    lane_table: tuple[_lanes.Lane, ...],
) -> tuple[bool, str | None, dict[str, Any] | None, str | None]:
    """``(go, lane name, stamp, fallback)`` for a reviewer's ``run_slice``:
    a lane of a vendor that did not build what it reviews, when available;
    ``go=False`` when no lane can take the review. ``stamp`` is the decide()
    shadow record for the pick on the minted job (td572552 S4b; ``None``
    under the default legacy mode).

    When the last attempt failed with a retryable Devin empty response,
    its lane is known, and the review has no model pin, first try flat-rate
    lanes outside every build vendor and the failed attempt's vendor.
    The usual sequence excludes every build vendor, then only the original
    builder's vendor, then tries the full table. Advance whenever the
    picker finds no available lane in the current table (gr730165).
    Filter the tables before picking because ``exclude_vendor`` can fall
    back to an excluded vendor.

    There is no per-lane review preference: inside a step the pick is
    :func:`~precis.plans.lanes.pick_lane`'s ``(tier, live / cap, live)``
    with ties in ``lane_rank`` order. That is why codex takes most
    reviews (gr761723, 761 of 1,212 verdicts in the week to 2026-11-22):
    devin and codex — the two cap-8 flat-rate lanes, devin ranked first —
    build most work, so the builder's vendor step usually leaves codex
    (cap 8, rank 20) beside claude-sub (cap 3, rank 30), and codex's
    larger cap keeps its load fraction lower.

    ``fallback`` is :func:`_review_fallback_lane` — another flat-rate
    lane of a vendor that built none of the work — or ``None``. The
    minted job carries it as
    :data:`~precis.plans.templates.REVIEW_FALLBACK_KEY`: a quota, rate or
    auth window on the chosen lane, or the pin going dead, moves the
    review there instead of parking it (``run_slice``); a full chosen
    lane is still waited for."""
    cls = _templates.REVIEW_TASK_CLASS
    vendors = _builder_vendors(plan, n, lane_table)
    counts = _lanes.live_lane_counts(w.store)
    backoff = _lanes.lane_backoffs(w.store)
    tables = list(_review_lane_tables(lane_table, vendors))
    attempts = _attempt_jobs(plan, n)
    last = attempts[-1] if attempts else None
    if (
        last is not None
        and last.status == "failed"
        and "model" not in _resolve_op(n)[1]
    ):
        previous = next(
            (ln for ln in lane_table if ln.name == last.meta.get("lane")), None
        )
        if (
            previous is not None
            and devin_retryable_agent_error(_job_event_text(w.store, last.id))
            == "empty_response"
        ):
            excluded = frozenset((*vendors, previous.vendor))
            alternatives = tuple(
                ln
                for ln in lane_table
                if ln.vendor not in excluded and ln.cost_class != _lanes.COST_METERED
            )
            tables.insert(0, (excluded, alternatives))
    for excluded, table in tables:
        lane = _lanes.pick_lane(
            w.store, cls, lanes=table, counts=counts, backoff=backoff
        )
        if lane is None:
            continue
        fallback = _review_fallback_lane(
            w.store, cls, lane_table, vendors, lane.name, backoff
        )
        if vendors and lane.vendor == vendors[0]:
            # Step three only: the original builder's own vendor reviews.
            res.notes.append(
                f"td{n.id}: review on lane {lane.name} shares the builder's vendor "
                f"{lane.vendor!r} (no other vendor has an available lane)"
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
        return True, lane.name, stamp, fallback
    res.dispatch_hold(n, f"review not dispatched — no available lane fits {cls!r}")
    return False, None, None, None


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


def _fix_siblings(plan: _Plan, n: _Node) -> list[_Node]:
    """The other fix nodes of ``n``'s iteration — ``run_slice`` todos
    ``blocked-by`` the same digest; the digest mints one per further
    cluster of the red (``templates.mint_release_fix_clusters``)."""
    digest = _digest_of(plan, n)
    if digest is None:
        return []
    return [
        m
        for m in plan.todos()
        if m.id != n.id
        and _op_type(m) == DEFAULT_OP
        and any(b.id == digest.id for b in plan.blockers.get(m.id, []))
    ]


def _may_still_land(plan: _Plan, m: _Node) -> bool:
    """A fix node that can still move the release head: open and not
    itself parked on the rerun, or closed with its land held or in
    flight."""
    if NOREPRO_WAIT_TAG in m.open_tags:
        return False
    if not _closed(m):
        return True
    return m.status == "done" and (
        plan.live_job(m) or isinstance(m.meta.get(_templates.PENDING_LAND_KEY), dict)
    )


def _landed_fix(store: Any, plan: _Plan, siblings: list[_Node]) -> _Node | None:
    """The first of ``siblings`` whose fix is on the release branch: a
    succeeded ``land_slice`` under it and a ``landed: <sha>`` comment
    (a land of an empty branch records ``landed: nothing``)."""
    for m in siblings:
        if any(
            j.meta.get("job_type") == "land_slice" and j.status == "succeeded"
            for j in plan.jobs(m)
        ) and _landed_sha(store, m.id):
            return m
    return None


def _owns_rerun(plan: _Plan, m: _Node, minted: set[int]) -> bool:
    """``m`` is parked on the rerun it fired (this pass, or earlier and
    not yet settled)."""
    if m.id in minted:
        return True
    return (
        NOREPRO_WAIT_TAG in m.open_tags
        and not _closed(m)
        and any(j.meta.get("job_type") == "ci_rerun" for j in plan.jobs(m))
    )


def _mint_rerun_ask(store: Any, n: _Node, why: str, continued: str) -> int | None:
    """The ONE ask a stalled rerun path files under the plan root
    (gr588028 item 4, review round 1): it RECORDS the stall — the loop
    already continued (or still waits) without it — so it is
    ``blocks: "nothing"`` with no ``blocked-by`` edge, and the only
    option is ``acknowledged``. ``why`` names the error TYPE only,
    never internals. The ``ci_rerun_stalled`` marker on the fix node
    is the dedupe."""
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
        if (
            digest is not None
            and not plan.live_job(digest)
            and digest.status not in _RULING_STATUSES
        ):
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
    the node out of dispatch while this step owns it.

    A red split into clusters has several fix nodes on one run
    (gr738684), so before a rerun mints: a sibling that landed closes
    the node (the head moved past the run; the push's gate run re-tests
    its ids), a sibling that may still land makes it wait, and a
    sibling that already fired the run's one rerun is waited on."""
    #: Nodes that minted a rerun this pass — ``plan`` was loaded before.
    minted: set[int] = set()
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
            # gr738684: a red split into clusters has one fix node per
            # cluster, each landing on the release branch. A sibling's
            # land moves the head past the red run — ``ci_rerun`` refuses
            # a superseded run, and the push started a gate run on the
            # new head that re-tests this node's ids. r339's td738093
            # ended "already fixed on origin/release/r339" and was parked
            # for a rerun that could never fire.
            siblings = _fix_siblings(plan, n)
            landed = _landed_fix(store, plan, siblings)
            if landed is not None:
                w.comment(
                    n,
                    f"validated: no rerun — td{landed.id} landed on the "
                    "release branch after the red run; the gate run on the "
                    "new head re-tests these ids",
                )
                w.remove_tags(n, [NOREPRO_WAIT_TAG])
                w.set_status(n, "done")
                res.notes.append(
                    f"td{n.id}: td{landed.id} landed after the red run; "
                    "closed without a rerun"
                )
                continue
            working = [m for m in siblings if _may_still_land(plan, m)]
            if working:
                # A rerun fired now is superseded the moment one of them
                # lands; the restamp waits for them anyway.
                res.notes.append(
                    f"td{n.id}: rerun waits — td{working[0].id} of the same "
                    "iteration may still land"
                    + (f" (+{len(working) - 1} more)" if len(working) > 1 else "")
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
                owner = next(
                    (m for m in siblings if _owns_rerun(plan, m, minted)), None
                )
                if owner is not None:
                    # Two clusters of one flaky run both ended non-repro:
                    # the run is rerun once, and its verdict settles both
                    # (green closes this node above; a stall un-parks the
                    # owner, then this node below).
                    res.notes.append(
                        f"td{n.id}: waits on the rerun of run {rid} that "
                        f"td{owner.id} fired"
                    )
                    continue
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
                meta_extra={
                    ATTEMPT_FREE_KEY: "ci-rerun",
                    # The park tag is a pause tag, and ssh_node's parent
                    # gate skips a job under one: the rerun names the tag
                    # as the wait it resolves, or it sits queued until a
                    # person removes the tag (gr738684, 51 and 63 min).
                    SERVES_PARENT_WAIT_KEY: NOREPRO_WAIT_TAG,
                },
            )
            minted.add(n.id)
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


@dataclass(frozen=True)
class _Started:
    """A build whose work has started and not landed (the overlap hold's
    blocker): ``state`` is ``landing``, ``in pre-land review`` or
    ``building``; ``branch_files`` the paths its branch touched when it
    last exited clean (:data:`~precis.plans.templates.BRANCH_FILES_KEY`),
    empty before then; ``repo`` the repo its ``repo:`` tag resolves to —
    its own, else the nearest tagged ancestor's — resolved in the store
    because a held build can sit outside the loaded plan's nodes."""

    state: str
    branch_files: tuple[str, ...]
    repo: str
    branch_files_current: bool = False


#: ``factory.overlap_hold_max_runs`` when unset or unreadable.
OVERLAP_HOLD_MAX_RUNS = 3


def _overlap_hold_max_runs(store: Any) -> int:
    """How many ended ``run_slice`` runs a build may have and still hold
    the file-overlap lock while it builds or waits for its pre-land
    review — ``factory.overlap_hold_max_runs``, default
    :data:`OVERLAP_HOLD_MAX_RUNS`; ``0`` never lifts the hold. Unreadable
    reads as the default: the claim transaction calls this, and a
    settings blip must not change who holds whom."""
    from precis import settings

    try:
        runs = settings.get_int("factory.overlap_hold_max_runs", store=store)
    except Exception:
        return OVERLAP_HOLD_MAX_RUNS
    return OVERLAP_HOLD_MAX_RUNS if runs is None else max(0, runs)


@overload
def _started_slices(
    store: Any, *, single_flight: Literal[False] = False
) -> dict[int, _Started]: ...


@overload
def _started_slices(
    store: Any, *, single_flight: Literal[True]
) -> dict[int, _Started] | None: ...


def _started_slices(
    store: Any, *, single_flight: bool = False
) -> dict[int, _Started] | None:
    """Every build with started, unlanded work, factory-wide (td603849,
    widened 2026-11-15 from in-flight lands): a live ``land_slice`` or
    ``run_slice`` job, or a finished build held for its pre-land review
    (``meta.pending_land``) — an hour of build plus review in which a
    second slice editing the same file used to start on a main that
    would not have the first. Generators (triage, brief, reviewer)
    commit nothing and never count. One query, not the loaded plan: a
    held build can sit outside the loaded subtrees. A terminal *ruling*
    on the todo (``_RULING_STATUSES`` — ``won't-do``, ``auto-timeout``;
    ``wontfix``/``cancelled`` are not todo statuses)
    ends the hold whatever a stale ``pending_land`` or a still-live job
    says: a dropped plan closing build and reviewer before a verdict
    never clears the stamp (only ``_record_land`` /
    ``_review_back_to_builder`` do), and the work is never landing, so
    it cannot hold overlapping builds forever. ``done`` still counts —
    its held land may be in flight.

    A bare ``pending_land`` stamp (no live job) holds only while the
    land can still come (gr701628): it is dropped when the build's
    newest ``land_slice`` job died (``failed``/``cancelled``/
    ``timed-out``) — nothing re-mints it and only ``_record_land`` clears
    the stamp, so the dead land held the lock forever — or when the build
    or an ancestor — or its pending pre-land review todo — carries a
    ``rescue:`` or ``child-failed:`` tag (the rescue pass owns it now;
    the rescue machinery tags the review, not the build, when it hands
    a stuck review to a human). The dead-land drop is scoped to the current
    build attempt (review td701778): the dying job carries the ``head``
    it was minted from (the stamp's pushed tip at mint time), and only a
    dead land OF THE CURRENT STAMP's head drops the hold — after a
    failed land the rebuild re-stamps a fresh ``pending_land`` with the
    new head while the historical dead job is still the newest one, and
    that stale failure must not release the hold during the rebuilt
    branch's pre-land review. Heads absent on both sides (a defer that
    could not resolve its tip) count as one attempt, the pre-scope read.
    A build still in its pre-land review (no land job yet) counts as
    before. Only a ``STATUS:running`` ``run_slice`` job or a
    ``land_slice`` job holds; a merely queued job holds nothing, so
    queued builds never block each other.

    The hold is bounded (:func:`_overlap_hold_max_runs`): a build whose
    ``run_slice`` runs ended that many times without a land has had its
    turns and holds only while it is ``landing``. A cancelled run and an
    attempt-free failure or host-refused launch are no turn. Three builds that did
    not converge with their pre-land reviewers (seven runs on one) held
    30 queued builds, the oldest for 23 hours, on 2026-11-19; a real
    conflict with the work that went ahead rebuilds at their land.

    ``single_flight`` (dispatch diagnostics only, gr732475): take a
    transaction advisory lock around the scan and return ``None`` when
    another scan holds it, so concurrent dispatch passes do not stack
    copies of the query. Claim and rescue callers leave it off and
    always run the full scan: a skipped scan there would drop real
    overlap holds."""
    with store.pool.connection() as conn, conn.transaction():
        if single_flight:
            lock = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_STARTED_SLICES_LOCK_KEY,)
            ).fetchone()
            if not lock or not lock[0]:
                return None
        rows = conn.execute(
            """
            WITH live AS (
                SELECT j.parent_id, bool_or(j.meta->>'job_type' = 'land_slice')
                       AS landing,
                       array_agg(DISTINCT j.meta->'params'->>'head')
                           FILTER (WHERE j.meta->>'job_type' = 'land_slice')
                           AS land_heads
                  FROM refs j
                 WHERE j.kind = 'job' AND j.retired_at IS NULL
                   AND j.meta->>'job_type' IN ('run_slice', 'land_slice')
                   AND j.parent_id IS NOT NULL
                   AND (j.meta->>'job_type' = 'land_slice' OR EXISTS (
                         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                            AND t.value = 'running'))
                   AND NOT EXISTS (
                         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                            AND t.value = ANY(%s))
                 GROUP BY j.parent_id
            ),
            candidates AS (
                SELECT parent_id AS ref_id FROM live
                UNION
                SELECT ref_id FROM refs
                 WHERE kind = 'todo' AND retired_at IS NULL
                   AND jsonb_typeof(meta->'pending_land') = 'object'
            )
            SELECT r.ref_id, COALESCE(live.landing, FALSE),
                   (jsonb_typeof(r.meta->'pending_land') = 'object'
                    AND NOT (COALESCE(land_dead.dead, FALSE)
                             AND land_dead.job_head IS NOT DISTINCT FROM
                                 (r.meta->'pending_land'->>'head'))),
                   r.meta->%s,
                   r.meta->>%s,
                   CASE WHEN live.landing THEN live.land_heads
                        ELSE ARRAY[r.meta->'pending_land'->>'head'] END,
                   (SELECT count(*) FROM refs j
                     WHERE j.kind = 'job' AND j.retired_at IS NULL
                       AND j.parent_id = r.ref_id
                       AND j.meta->>'job_type' = 'run_slice'
                       AND NOT COALESCE(j.meta ? %s, FALSE)
                       AND NOT COALESCE(j.meta ? 'host_refusal', FALSE)
                       AND EXISTS (
                             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                              WHERE rt.ref_id = j.ref_id
                                AND t.namespace = 'STATUS'
                                AND t.value = ANY(%s)))
              FROM candidates c
              JOIN refs r ON r.ref_id = c.ref_id
              LEFT JOIN live ON live.parent_id = r.ref_id
              LEFT JOIN LATERAL (
                  SELECT EXISTS (
                             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                              WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                                AND t.value = ANY(%s)) AS dead,
                         j.meta->'params'->>'head' AS job_head
                    FROM refs j
                   WHERE j.kind = 'job' AND j.retired_at IS NULL
                     AND j.meta->>'job_type' = 'land_slice'
                     AND j.parent_id = r.ref_id AND j.parent_id IS NOT NULL
                     AND jsonb_typeof(r.meta->'pending_land') = 'object'
                   ORDER BY j.ref_id DESC LIMIT 1
              ) land_dead ON TRUE
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND NOT COALESCE(r.meta ? 'generate', FALSE)
               AND (live.parent_id IS NOT NULL
                    OR NOT (COALESCE(land_dead.dead, FALSE)
                            AND land_dead.job_head IS NOT DISTINCT FROM
                                (r.meta->'pending_land'->>'head')))
               AND NOT EXISTS (
                         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                            AND t.value = ANY(%s))
            """,
            (
                sorted(_JOB_TERMINAL),
                _templates.BRANCH_FILES_KEY,
                _templates.BRANCH_FILES_HEAD_KEY,
                ATTEMPT_FREE_KEY,
                sorted(_JOB_TERMINAL - {"cancelled"}),
                sorted(_JOB_TERMINAL - {"succeeded"}),
                sorted(_RULING_STATUSES),
            ),
        ).fetchall()
    out: dict[int, _Started] = {}
    # Read the cap only when there is a holder to judge.
    max_runs = _overlap_hold_max_runs(store) if rows else 0
    for rid, landing, pending, files, stamp_head, land_heads, runs in rows:
        if max_runs and not landing and int(runs or 0) >= max_runs:
            continue
        state = (
            "landing" if landing else "in pre-land review" if pending else "building"
        )
        paths = tuple(str(p) for p in files) if isinstance(files, list) else ()
        current = (
            bool(stamp_head)
            and land_heads == [stamp_head]
            and isinstance(files, list)
            and all(isinstance(p, str) for p in files)
        )
        out[int(rid)] = _Started(state, paths, _lanes.DEFAULT_REPO, current)
    if not out:
        return out
    # A bare pending_land stamp under a rescue / child-failed tag (own, an
    # ancestor's, or its pending pre-land review's) holds no lock (gr701628).
    for rid in _rescued_ids(
        store, [i for i, s in out.items() if s.state == "in pre-land review"]
    ):
        del out[rid]
    if not out:
        return out
    # The blocker side of the repo check: ``_node_repos``' reach (own
    # ``repo:`` tags, else the nearest tagged ancestor's) resolved in the
    # store, since the loaded plan cannot see an out-of-subtree todo.
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE up(origin, anc, depth) AS (
                SELECT ref_id, ref_id, 0
                  FROM refs WHERE ref_id = ANY(%s)
                UNION ALL
                SELECT u.origin, r.parent_id, u.depth + 1
                  FROM up u JOIN refs r ON r.ref_id = u.anc
                 WHERE u.anc IS NOT NULL AND u.depth < 25
            ),
            tagged AS (
                SELECT u.origin, t.value, u.depth,
                       MIN(u.depth) OVER (PARTITION BY u.origin) AS nearest
                  FROM up u
                  JOIN refs r ON r.ref_id = u.anc
                  JOIN ref_tags rt ON rt.ref_id = r.ref_id
                  JOIN tags t USING (tag_id)
                 WHERE r.kind = 'todo' AND r.retired_at IS NULL
                   AND t.namespace = 'OPEN' AND starts_with(t.value, 'repo:')
            )
            SELECT origin, value FROM tagged WHERE depth = nearest
            """,
            (sorted(out),),
        ).fetchall()
    repos: dict[int, list[str]] = {}
    for origin, value in rows:
        repos.setdefault(int(origin), []).append(str(value)[len("repo:") :].strip())
    return {
        rid: _Started(
            s.state,
            s.branch_files,
            sorted(repos[rid])[0] if rid in repos else _lanes.DEFAULT_REPO,
            s.branch_files_current,
        )
        for rid, s in out.items()
    }


def _rescued_ids(store: Any, ids: list[int]) -> set[int]:
    """The todos in ``ids`` that the rescue pass owns: one that carries a
    ``rescue:`` or ``child-failed:`` open tag itself or on an ancestor,
    or whose pending pre-land review does. The review is the reviewer
    todo ``meta.generate = {role: reviewer, from: td<id>}`` with
    ``meta.preland_review`` (the lookup of ``run_slice._preland_reviewer``),
    with ``from`` naming the build or, for a slicer's slice, its
    parent (:func:`_review_targets`). The rescue machinery tags the
    review, not the build, when it hands a stuck review to a human, and
    the build behind it parks ``done`` with ``pending_land`` set: without
    this arm it held the file-overlap gate for as long as the human took.
    Read in the store, because a held build can sit outside the loaded
    plan; :func:`_held_by_registry` is the in-plan read of the same tags
    for a node being dispatched, and a stamped build is closed, so it
    needs no such arm."""
    if not ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE up(origin, anc, depth) AS (
                SELECT ref_id, ref_id, 0
                  FROM refs WHERE ref_id = ANY(%s)
                UNION ALL
                SELECT u.origin, r.parent_id, u.depth + 1
                  FROM up u JOIN refs r ON r.ref_id = u.anc
                 WHERE u.anc IS NOT NULL AND u.depth < 25
            )
            SELECT u.origin
              FROM up u
              JOIN refs r ON r.ref_id = u.anc
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN'
               AND (starts_with(t.value, 'rescue:')
                    OR starts_with(t.value, 'child-failed:'))
            UNION
            SELECT b.ref_id
              FROM refs b
              JOIN refs rv
                ON rv.kind = 'todo' AND rv.retired_at IS NULL
               AND rv.meta->'generate'->>'role' = 'reviewer'
               AND rv.meta->%s = 'true'::jsonb
               AND rv.meta->'generate'->>'from'
                   IN ('td' || b.ref_id::text, 'td' || b.parent_id::text)
              JOIN ref_tags rt2 ON rt2.ref_id = rv.ref_id
              JOIN tags t2 ON t2.tag_id = rt2.tag_id
             WHERE b.ref_id = ANY(%s)
               AND t2.namespace = 'OPEN'
               AND (starts_with(t2.value, 'rescue:')
                    OR starts_with(t2.value, 'child-failed:'))
            """,
            (sorted(ids), _templates.PRELAND_REVIEW_KEY, sorted(ids)),
        ).fetchall()
    return {int(r[0]) for r in rows}


#: ``scripts/ship`` narrates its own steps on lines led by one of these
#: marks (``▶ rebuilding Tailwind CSS``, ``→ src/precis_web/static/
#: tailwind.css``, ``⚠ deploy lag …``).
_SHIP_NARRATION_MARKS = ("▶", "→", "⚠")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _without_ship_narration(text: str) -> str:
    """A lander handoff minus the lines ``scripts/ship`` wrote about its
    own steps. The handoff quotes the gate's output before the verdict,
    and every land rebuilds the stylesheet, so ``→ …/tailwind.css`` made
    each build with a failed land declare that file and wait on every
    other one (gr736403). Any other comment is returned as written: a
    brief may open a line with an arrow."""
    from precis.workers.job_types import run_slice

    if not text.lstrip().lower().startswith(run_slice._LANDER_HANDOFF_PREFIX):
        return text
    return "\n".join(
        line
        for line in text.splitlines()
        if not _ANSI_RE.sub("", line).lstrip().startswith(_SHIP_NARRATION_MARKS)
    )


def _overlap_inputs(
    store: Any, ids: list[int]
) -> dict[int, tuple[frozenset[str], str]]:
    """Read file sets and effective land targets on the claim connection.

    The file set is what the todo declares (gr734976): its body, its
    ``brief:`` comments, the machine-written handoffs that name files to
    fix (the lander's failure note without ship's own narration,
    :func:`_without_ship_narration`, and the pre-land hand-back,
    :func:`~precis.workers.job_types.run_slice._outranks_brief`) and its
    own stamped ``meta.branch_files``. A builder session's plain
    ``handoff:`` names files it read, ran or found red, not files it
    edits, so it contributes none. Target resolution still reads the
    newest of every brief and handoff, as build and land do.
    """
    from precis.workers.job_types import run_slice

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH inputs AS (
                SELECT r.ref_id, r.meta->%s AS target,
                       r.meta->%s AS branch_files, ARRAY(
                    SELECT t.value
                      FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                       AND starts_with(t.value, 'attempt:')
                ) AS retry_tags
                  FROM refs r WHERE r.ref_id = ANY(%s)
            )
            SELECT i.ref_id, i.target, i.retry_tags, i.branch_files,
                   c.ord, c.chunk_kind, c.text
              FROM inputs i
              LEFT JOIN chunks c ON c.ref_id = i.ref_id AND c.ord >= 0
                   AND c.chunk_kind IN (%s, %s)
             ORDER BY i.ref_id, c.ord
            """,
            (
                _release.LAND_TARGET_KEY,
                _templates.BRANCH_FILES_KEY,
                ids,
                TODO_BODY_KIND,
                _COMMENT_KIND,
            ),
        ).fetchall()

    result: dict[int, tuple[frozenset[str], str]] = {}
    for rid, group in groupby(rows, key=itemgetter(0)):
        ref_rows = list(group)
        _, target, tags, stamped, *_ = ref_rows[0]
        body = "\n\n".join(
            text
            for _, _, _, _, _, kind, text in ref_rows
            if kind == TODO_BODY_KIND and text
        )
        briefs = [
            (ord_, text)
            for _, _, _, _, ord_, kind, text in ref_rows
            if kind == _COMMENT_KIND
            and text
            and text.lstrip().lower().startswith(run_slice._BRIEF_PREFIXES)
        ]
        latest, _latest_ord = run_slice._select_slice_brief(
            briefs, reviewer=False, open_tags=partial(iter, tags)
        )
        meta = {_release.LAND_TARGET_KEY: target}
        # gr734976: a builder's own ``handoff:`` is prose about files it
        # read or found red; only briefs and the machine's file-naming
        # handoffs, plus the stamped branch files, say what it touches.
        declared = [
            _without_ship_narration(text)
            for _, text in briefs
            if text.lstrip().lower().startswith("brief:")
            or run_slice._outranks_brief(text.lstrip())
        ]
        if isinstance(stamped, list):
            declared.extend(str(p) for p in stamped)
        files = _dscore.brief_files("\n".join([body, *declared]))
        result[rid] = files, _release.land_target_of(meta, body, latest) or "main"
    return result


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


def _newest_brief_chunk(n: _Node, chunks: list[Any]) -> Any | None:
    """The newest coordinator ``brief:`` comment chunk of ``n`` — not
    worker-stamped, not hidden — that :func:`_route_text` reads as the
    brief; shared so the pre-land finding route can date it
    (gr738860)."""
    from precis.workers.job_types import run_slice as _rs

    hidden = set(n.meta.get(_HIDDEN_META_KEY) or ())
    return next(
        (
            c
            for c in sorted(chunks, key=lambda c: int(c.ord), reverse=True)
            if c.chunk_kind == _rs._COMMENT_KIND
            and int(c.ord) not in hidden
            and not str((c.meta or {}).get("principal", "")).startswith("worker:")
            and str(c.text or "").lstrip().lower().startswith("brief:")
        ),
        None,
    )


#: The hand-back's trailing ``(review td<N>, <mark> — rebuild on it …)``
#: parenthetical (:func:`_review_back_to_builder`): the tick's own
#: boilerplate, not part of the finding. Every stored mark matches —
#: the emitted one and the tokens earlier engines wrote.
_HANDBACK_TAIL_RE = re.compile(
    r"\n*\(review td\d+, (?:"
    + "|".join(re.escape(m) for m in _templates.PRELAND_HANDBACK_MARKS)
    + r").*\)\s*\Z",
    re.DOTALL,
)
#: The hand-back's ``handoff: review: <KIND> —`` head.
_HANDBACK_HEAD_RE = re.compile(
    r"\Ahandoff:\s*review:\s*(?:FIX|REVERT)?\s*[—–-]*\s*", re.IGNORECASE
)


def _handback_finding(
    store: Any,
    plan: _Plan,
    n: _Node,
    w: _Writer | None = None,
    *,
    after_rebuild: bool = False,
) -> str | None:
    """The finding of the newest pre-land hand-back on ``n`` still
    waiting for its rebuild (gr738860), else ``None``: the newest comment
    starting ``handoff: review:`` that carries a mark of
    :data:`_templates.PRELAND_HANDBACK_MARKS`, its head and trailing
    parenthetical stripped. ``None`` once a coordinator ``brief:``
    (:func:`_route_text`'s) is newer — the coordinator has spoken — or a
    builder job was created after it (a rebuild already ran on that
    finding), unless checking a no-op verdict, which requires a rebuild.
    A dry pass reads a hand-back it would have written from
    ``w.dry_comments`` when looking for work to dispatch."""
    if not after_rebuild and w is not None and w.dry_run:
        for ref_id, text in reversed(w.dry_comments):
            if (
                ref_id == n.id
                and text.lstrip().lower().startswith("handoff: review:")
                and any(m in text for m in _templates.PRELAND_HANDBACK_MARKS)
            ):
                text = _HANDBACK_TAIL_RE.sub("", text.strip())
                return _HANDBACK_HEAD_RE.sub("", text, count=1).strip() or None
    hand = next(
        (
            c
            for c in _comments(store, n.id)
            if str(c.text).lstrip().lower().startswith("handoff: review:")
            and any(m in str(c.text) for m in _templates.PRELAND_HANDBACK_MARKS)
        ),
        None,
    )
    stamp = getattr(hand, "created_at", None)
    if hand is None or not isinstance(stamp, datetime):
        return None
    from precis.workers.job_types import run_slice as _rs

    brief = _newest_brief_chunk(n, _rs._todo_chunks(store, n.id))
    brief_at = getattr(brief, "created_at", None)
    if brief is not None and (not isinstance(brief_at, datetime) or brief_at > stamp):
        return None
    rebuilt = any(
        str(j.meta.get("job_type") or DEFAULT_OP) == DEFAULT_OP and j.created_at > stamp
        for j in plan.jobs(n)
    )
    if rebuilt != after_rebuild:
        return None
    text = _HANDBACK_TAIL_RE.sub("", str(hand.text).strip())
    return _HANDBACK_HEAD_RE.sub("", text, count=1).strip() or None


def _as_intent(finding: str) -> str:
    """``finding`` with every paragraph under a ``Fix:`` label, so the
    classifier's change-intent read (``route._INTENT_START``) sees all of
    it, not just the first paragraph (gr738860). Fenced blocks stay whole;
    a paragraph that already opens with an intent or evidence label, a
    quote or a heading keeps its own reading."""
    out: list[str] = []
    fence = ""
    fresh = True
    for line in finding.splitlines():
        bare = line.strip()
        if fence:
            if bare.startswith(fence):
                fence = ""
        elif match := _route._FENCE.match(bare):
            fence = match.group(1)
        elif not bare:
            fresh = True
            out.append(line)
            continue
        if (
            fresh
            and not fence
            and not bare.startswith((">", "#"))
            and not _route._INTENT_START.match(bare)
            and not _route._EVIDENCE_START.match(bare)
            and not _route._FENCE.match(bare)
        ):
            line = "Fix: " + line
        fresh = False
        out.append(line)
    return "\n".join(out)


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
    newest = _newest_brief_chunk(n, chunks)
    brief = str(newest.text).lstrip() if newest is not None else None
    if brief is not None:
        # The ``brief:`` label is the comment's type, not instruction
        # text: judged with the label still on it, a handle-only brief
        # carries one letter token too many and never classifies
        # (td645566).
        brief = brief[len("brief:") :].lstrip()
    return "\n\n".join(parts), brief


def _block_route(
    plan: _Plan,
    w: _Writer,
    res: PassResult,
    n: _Node,
    why: str,
    text: str,
    lead: str = "brief",
) -> None:
    """Hold one run_slice-bound node on a coordinator ask — the mint-time
    route of gr645163: a brief no builder lane may run (it asks to write
    a deny path, or the fix is prod-graph only — gr685970) mints no job
    and burns no build; the ``blocked-by`` edge keeps later passes from
    re-minting or re-asking. The ask's body carries the brief itself —
    the ruling's "with the diff/brief attached", and ``why`` quotes the
    sentence the route fired on. ``lead`` names the judged text in the
    body and dry-run note (``brief``; for a pre-land review finding that
    needs a denied path, gr738860, the hand-back's)."""
    now = datetime.now(UTC)
    if w.dry_run:
        # Mirror the hold so hygiene cannot report a second ask this pass.
        plan.blockers.setdefault(n.id, []).append(
            _Blocker(id=-n.id, status="open", is_ask=True, created_at=now)
        )
        res.notes.append(f"td{n.id}: would ask coordinator — {lead} {why}")
    else:
        ask_id = _mint_coordinator_ask(
            w.store,
            plan,
            n,
            title=f"td{n.id}: {why} — a coordinator call",
            body=(
                f"Task td{n.id} routes to the coordinator at mint: the "
                f"{lead} {why}. No builder lane may do the work, so no "
                "run_slice job was minted — the builder that ran it would "
                "refuse as won't-do or end with no commit (gr645163, the "
                "td643078/td638980 stalls). Land it by hand, mint a "
                "builder outside the factory's deny rules, or drop the "
                + (
                    f"node; closing this ask `acknowledged` re-arms td{n.id} "
                    "and it routes again while the text still asks to write "
                    "a denied path."
                    if lead == "brief"
                    else f"node. A newer `brief:` comment on td{n.id} "
                    "replaces the review finding as the builder's "
                    "instruction — say what it may do inside its lanes, or "
                    "that the denied-path part was landed by hand — and "
                    f"closing this ask `acknowledged` then rebuilds td{n.id} "
                    "on it. Closing `acknowledged` with no newer `brief:` "
                    "routes here again (gr738860)."
                )
                + "\n\n---\n\n"
                + text.strip()[:4000]
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


def _review_repo_mismatch(plan: _Plan, n: _Node) -> str | None:
    """Why the reviewer ``n`` must not dispatch: the repo it resolves to
    (:func:`_node_repos`, none = the default) is not the repo its review
    target(s) landed in (``meta.landed_repo``, absent = the default).
    ``None`` when they agree. A reviewer with no target in the plan (a
    sha-keyed one) compares against the default repo."""
    tagged = _node_repos(plan, n)
    own = tagged[0] if tagged else _lanes.DEFAULT_REPO
    landed = {
        str(t.meta.get(_lanes.LANDED_REPO_KEY) or _lanes.DEFAULT_REPO)
        for t in _review_targets(plan, n)
    } or {_lanes.DEFAULT_REPO}
    if landed == {own}:
        return None
    return (
        f"review repo mismatch: the reviewer resolves to repo {own}, its "
        f"target landed in {', '.join(sorted(landed))}"
    )


def _same_red_digest(plan: _Plan, n: _Node, other: _Node | None) -> bool:
    """``n`` and ``other`` are fix nodes of one red run: both
    ``blocked-by`` the same ``ci_red_digest`` node (:func:`_digest_of`).
    Their briefs all carry that run's whole digest and changed-files
    table (``ci_red_digest._cluster_briefs``), so each names every file
    its siblings' clusters name: the file-overlap hold read them as one
    conflict and ran the fixers of a red release one at a time, each
    waiting for the one before it to land. They build side by side
    instead (Reto 2026-11-19). The lands still go one at a time: ship's
    round lock admits one release push, and a land that finds it held
    runs again a minute later (``land_slice.RELEASE_BUSY_MARK``); a fix
    that conflicts with a sibling's goes back to its builder at the
    land, like any conflicting land. Fix nodes of different iterations
    wait on different digests and keep the hold."""
    if other is None or other.id == n.id:
        return False
    mine = _digest_of(plan, n)
    theirs = _digest_of(plan, other)
    return mine is not None and theirs is not None and mine.id == theirs.id


def _held_files(s: _Started, declared: frozenset[str]) -> frozenset[str]:
    """The files a started build holds. Once it has left the build step
    with a complete stamp for its landing or review tip, the branch diff
    is the whole answer: a file its body or a lander note
    merely names is not one it edits (gr736403: a landing build with four
    branch files held 16 queued builds through two it never touched).
    While it builds or the stamp is missing, incomplete or stale, the
    declared files count too."""
    branch = _dscore.brief_files("\n".join(s.branch_files))
    if s.branch_files_current and s.state != "building":
        return branch
    return declared | branch


def _overlap_holds(
    plan: _Plan,
    store: Any,
    started: dict[int, _Started],
    candidates: list[_Node],
) -> dict[int, tuple[int, frozenset[str]]]:
    """``{candidate id: (blocking started id, shared file keys)}`` — the
    td603849 file-overlap hold, reported by :func:`_step_dispatch` and
    enforced inside the executor claim transaction (gr702610). Only a default-op,
    non-generator build is held; ``started`` is :func:`_started_slices`;
    a started build blocks a candidate in the same repo when a file the
    candidate declares is the same repo-relative path as one the
    started build touches. Each side's declared files are its body, its
    ``brief:`` comments, the lander's failure handoff and the pre-land
    hand-back, and its stamped ``branch_files`` (so a retry that already
    has commits carries them); a builder's plain ``handoff:`` names files
    it merely read and counts for neither (gr734976;
    :func:`_overlap_inputs`,
    :func:`~precis.utils.dispatch_score.shared_files`). A brief
    that names a file by a shorter suffix counts only when exactly one of
    the started build's paths ends with it — the same basename in two
    directories, or an ambiguous ``__init__.py``, holds nothing; a real
    conflict still rebuilds at land. Only started work holds, never
    another candidate, so two candidates never hold each other. Builds
    hold each other only on the same land target (gr688291), and the
    fix nodes of one red digest never hold each other
    (:func:`_same_red_digest`).
    One ``_overlap_inputs`` query for the lot; empty without a started
    build or a build candidate."""
    builds = [
        n
        for n in candidates
        if _resolve_op(n)[0] == DEFAULT_OP and not _is_generator(n)
    ]
    if not started or not builds:
        return {}
    inputs = _overlap_inputs(store, list(started) + [n.id for n in builds])
    default: tuple[frozenset[str], str] = (frozenset(), "main")
    held: dict[int, tuple[int, frozenset[str]]] = {}
    held_sets = {
        i: _held_files(s, inputs.get(i, default)[0]) for i, s in started.items()
    }
    for n in builds:
        mine, target = inputs.get(n.id, default)
        if not mine:
            continue
        repo = _node_repo(plan, n)
        for hid in sorted(held_sets):
            # started[hid].repo, not _node_repo on plan.nodes: a held
            # build can sit outside the loaded subtrees, and skipping
            # the check then held a same-path build in another repo.
            if started[hid].repo != repo:
                continue
            # gr688291: another land target is another branch; a release
            # fix's wide digest brief must not wait on unrelated main work.
            if inputs.get(hid, default)[1] != target:
                continue
            if _same_red_digest(plan, n, plan.nodes.get(hid)):
                continue
            shared = _dscore.shared_files(mine, held_sets[hid])
            if hid != n.id and shared:
                held[n.id] = (hid, shared)
                break
    return held


def _node_deny_paths(store: Any, plan: _Plan, n: _Node) -> tuple[str, ...]:
    """The deny paths governing this build's repository."""
    repo = _node_repo(plan, n)
    if repo == _lanes.DEFAULT_REPO:
        return _lanes.deny_paths_for(store) + _governs.governed_skill_paths()
    profile = _lanes.repo_profile(repo)
    if profile is not None and profile.deny_paths is not None:
        return profile.deny_paths
    return _lanes.deny_paths_for(store)


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
    candidates = [
        n
        for n in sorted(plan.todos(), key=lambda x: (-_effective_score(plan, x), x.id))
        if _dispatchable(plan, n) and n.status == "open"
    ]
    from precis.workers.executors._common import source_holds as _read_source_holds

    waiting = [
        n
        for n in candidates
        if not (_excluded(plan, n) or _claims(n))
        and not any(j.status == "running" for j in plan.jobs(n))
    ]
    build_ids = [n.id for n in waiting if _resolve_op(n)[0] == DEFAULT_OP]
    source_holds = {}
    if build_ids:
        with w.store.pool.connection() as conn:
            source_holds = _read_source_holds(conn, build_ids)
    # File overlap remains a claim-time hold. Minting behind a source
    # (gripe or todo) hold, by contrast, would leave a job waiting
    # indefinitely.
    started = _started_slices(w.store, single_flight=True) if candidates else {}
    held = _overlap_holds(plan, w.store, started, waiting) if started else {}
    # gr657066: governs:factory skills join the routing set — a brief
    # naming one routes to the coordinator like any deny-path text.
    deny = _lanes.deny_paths_for(w.store) + _governs.governed_skill_paths()
    for n in waiting:
        if not (plan.blocked(n) or plan.paused(n)):
            brief_hold = _awaits_brief(plan, n, w)
            if brief_hold is not None:
                # A source-todo build starts from its brief alone: with
                # the brief node closed or unblocked by hand and no
                # ``brief:`` comment posted, it still does not run; a
                # stale one — the source moved after the writer read —
                # sends the chain back around for a fresh brief.
                res.dispatch_hold(
                    n,
                    "not dispatched — no `brief:` comment on it yet; "
                    "waits for its brief"
                    if brief_hold == "missing"
                    else "not dispatched — its brief is stale (the source "
                    "changed); re-briefing",
                )
                if brief_hold == "stale":
                    _rebrief_stale_brief(plan, n, w, res)
                continue
        if n.id in source_holds and _reland_params(plan, n, w.store) is None:
            if not plan.live_job(n) or any(
                j.status == "queued" and j.meta.get("job_type") == DEFAULT_OP
                for j in plan.jobs(n)
            ):
                res.dispatch_hold(n, source_holds[n.id])
                continue
        if (
            started is not None
            and n.id in held
            and not (plan.blocked(n) or plan.paused(n))
        ):
            res.held += 1
            hid, shared = held[n.id]
            named = sorted(p for p in shared if "/" in p) or sorted(shared)
            res.dispatch_hold(
                n,
                f"file-overlap hold — waits on td{hid} ({started[hid].state}): both touch "
                f"{', '.join(f'`{p}`' for p in named[:3])}"
                f"{' …' if len(named) > 3 else ''}; claims once td{hid} is on main",
            )
        if started is None and not (plan.blocked(n) or plan.paused(n)):
            # A skipped scan cannot tell whether a queued hold cleared.
            previous = n.meta.get(DISPATCH_HOLD_KEY)
            reason = previous.get("reason") if isinstance(previous, dict) else None
            if isinstance(reason, str) and reason.startswith("file-overlap hold —"):
                res.dispatch_holds[n.id] = reason
        if plan.live_job(n):
            continue
        # td659522 step 0: every op on a node tagged for another repo is
        # skipped before any routing, escalation or ask — the builder,
        # reviewer and lander all assume a precis checkout.
        repo_problem = _lanes.repo_refusal(_node_repos(plan, n), w.store)
        if repo_problem is not None:
            res.dispatch_hold(n, f"not dispatched — {repo_problem}")
            continue
        if _is_reviewer(n):
            # td659522 step 5: a reviewer runs in the repo its target landed
            # in; a mismatch would review one repo's sha in another's tree.
            # A blocked or paused one is skipped first, quietly: until its
            # target lands, the target has no landed_repo to agree with.
            if plan.blocked(n) or plan.paused(n):
                continue
            mismatch = _review_repo_mismatch(plan, n)
            if mismatch is not None:
                res.dispatch_hold(n, f"not dispatched — {mismatch}")
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
                    node_deny = _node_deny_paths(w.store, plan, n)
        if plan.blocked(n) or plan.paused(n):
            continue
        if _is_repo_release(n):
            # td659522 step 3: the release job cuts a tag in a second
            # repo; it takes the node's repo like a build does. A node
            # that resolves to precis or to nothing is not its business
            # (precis releases go through release_cut).
            tagged = _node_repos(plan, n)
            if not tagged or tagged[0] == _lanes.DEFAULT_REPO:
                res.dispatch_hold(
                    n,
                    f"{REPO_RELEASE_OP} not dispatched — no "
                    "second-repo repo: tag; precis releases go through "
                    "release_cut",
                )
                continue
            params["repo"] = tagged[0]
            params.setdefault("todo_id", n.id)
        if _is_pin_bump(n):
            # td659522 step 4: the pin-bump job names its release node and
            # the repo it pins; it builds in precis (no ``repo`` param).
            params, pin_problem = _pin_params(plan, n, params)
            if pin_problem is not None:
                res.dispatch_hold(n, f"{PIN_BUMP_OP} not dispatched — {pin_problem}")
                continue
            if _pin_awaits_land(plan, n):
                res.dispatch_hold(
                    n, f"{PIN_BUMP_OP} succeeded; waiting for its land_slice"
                )
                continue
            # td659522 F7: after a land the next job is the gate image wait;
            # it checks the image on the host the land ran on.
            land_host = _pin_land_host(plan, n)
            if land_host is not None:
                params.setdefault("target_node", land_host)
        reland = _reland_params(plan, n, w.store)
        if job_type == DEFAULT_OP and reland is None:
            task_class = str(n.meta.get("task_class") or _DEFAULT_TASK_CLASS)
            gap = _lanes.missing_class_reason(lane_table, task_class)
            if gap is not None:
                _block_lane_gap(plan, w, res, n, gap)
                continue
            # gr645163 (Reto 2026-11-12, "escalate to next model"): work
            # no builder lane may write — the judged text asks to write a
            # deny path (gr685970), or the fix is prod-graph only — skips
            # the builders and files one coordinator ask at mint.
            # Generators never classify: they read source text, and a
            # gripe or diff may name deny paths legitimately.
            if node_kind(n.meta) != "generator":
                body, brief = _route_text(w.store, n)
                routed = _route.coordinator_route(body, node_deny, brief=brief)
                if routed is not None:
                    text = "\n\n".join(part for part in (body, brief) if part)
                    _block_route(plan, w, res, n, routed, text)
                    continue
                # gr738860: a pre-land review FIX that needs a denied
                # path is no rebuild the builder lanes can do — the
                # worker-stamped hand-back never classifies above
                # (:func:`_route_text`), so a finding of this kind looped
                # rebuild -> review -> FIX until the round bound asked.
                # One ask holds the node (the ``blocked-by`` edge keeps
                # later passes quiet) while the hand-back awaits its
                # rebuild with no newer coordinator brief.
                finding = _handback_finding(w.store, plan, n, w)
                # The classifier reads change intent only under a ``Fix:``
                # / ``Goal:`` / ``Want:`` label (``route._INTENT_START``);
                # a review's finding is prose with no label, and it IS
                # what the build must fix — judge it as that.
                routed = (
                    _route.coordinator_route(
                        _as_intent(finding), node_deny, graph=False
                    )
                    if finding is not None
                    else None
                )
                if finding is not None and routed is not None:
                    _block_route(
                        plan,
                        w,
                        res,
                        n,
                        "review finding " + routed,
                        finding,
                        lead="pre-land hand-back's",
                    )
                    continue
        rungs = _task_rungs(lane_table, n, params) if job_type == DEFAULT_OP else ()
        cap = _retry_cap(n, _cap_rungs(plan, lane_table, n, job_type, params, rules))
        spend_hit = (
            _spend_hit(plan, n, _spend_rungs(lane_table, n, params), rules)
            if rules and job_type == DEFAULT_OP
            else None
        )
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
        if _capped(
            plan, n, cap, rungs=rungs, spend_capped=spend_hit is not None, now=now
        ):
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
                res.dispatch_hold(
                    n, f"newest job:{newest.id} was cancelled; not redispatched"
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
            ready = free_backoff_ready_at(newest.created_at, run)
            if ready is not None and ready > now:
                res.dispatch_hold(
                    n,
                    f"held — '{free_reason}' free failure "
                    f"x{run} in a row; next dispatch at {_iso(ready)}",
                )
                continue
            res.notes.append(f"td{n.id}: re-queued after {free_reason}, not an attempt")
        if reland is not None:
            # An infra-red land re-runs ship on the same branch with the
            # failed job's own params — no builder, no escalate, no review
            # lane (gr570187).
            job_type, params = "land_slice", reland
            if _conflict_land_note(plan, w.store, res, n, now) is not None:
                continue
        # gr741695: a land conflict's resolution attempt. Its tries are the
        # conflict path's own (``meta.conflict_tries``), kept off the
        # ordinary attempt count (:func:`_attempt_jobs`) and off the ladder:
        # the mechanical-red brief and the ``_escalate`` climb below both
        # skip a task whose conflict try is due, and the rung is pinned here
        # (:func:`_conflict_pin`).
        conflict_try = 0
        if job_type == DEFAULT_OP and reland is None and not _is_reviewer(n):
            cstate, ctry = _conflict_state(plan, n)
            if cstate == "try":
                conflict_try = ctry
        decision_stamp: dict[str, Any] | None = None
        review_fallback: str | None = None
        if _is_reviewer(n) and job_type == DEFAULT_OP and "lane" not in params:
            go, lane_name, decision_stamp, review_fallback = _review_lane(
                plan, w, res, n, lane_table
            )
            if not go:
                continue
            params["lane"] = lane_name
        mech_pinned = False
        repeated_lint = False
        if conflict_try:
            go, pin_note, unusable = _conflict_pin(
                plan, w, n, params, lane_table, rules, conflict_try
            )
            if pin_note:
                res.notes.append(pin_note)
            if unusable is not None:
                # Waiting would not clear it: the tries are spent, and the
                # hygiene step files the ask saying why.
                w.stamp_meta(
                    n,
                    {
                        CONFLICT_TRIES_KEY: {
                            **_dict_of(n.meta.get(CONFLICT_TRIES_KEY)),
                            "n": conflict_try,
                            "unusable": unusable,
                        }
                    },
                )
            if not go:
                continue
            params[CONFLICT_KEY] = _conflict_params(n, conflict_try)
            mech_pinned = True  # no ladder climb: the rung is the pin's
        elif _is_reviewer(n) and job_type == DEFAULT_OP:
            if (resolution := _resolution_param(plan, n)) is not None:
                params["resolution"] = resolution
        if (
            job_type == DEFAULT_OP
            and reland is None
            and requeued_cancel is None
            and not _is_reviewer(n)
            and not conflict_try
        ):
            # Lint gets one briefed retry with the gate's error lines.
            # A repeat uses the ordinary ladder and the lander's handoff.
            # Other mechanical reds get two briefed same-rung retries
            # (gr604579). The third stamps exhausted for the hygiene ask.
            lint = _lint_red_tail(plan, w.store, n)
            repeated_lint = len(lint) == 2
            mech: tuple[str, str, str, int, int] | None = None
            if len(lint) == 1:
                lint_job, errors = lint[0]
                mech = (
                    "lint",
                    "Fix the lint errors on this branch.",
                    errors,
                    1,
                    lint_job,
                )
            elif not lint:
                mech = _mech_red(plan, w.store, n)
            if mech is not None:
                slug, brief, line, repeats, last_job = mech
                stamp = {
                    "sig": slug,
                    "n": repeats,
                    "job": last_job,
                    "at": _iso(datetime.now(UTC)),
                }
                brief_cap = 1 if lint else MECH_RETRY_CAP
                if repeats > brief_cap:
                    w.stamp_meta(n, {MECH_RETRIES_KEY: {**stamp, "exhausted": True}})
                    res.dispatch_hold(
                        n,
                        f"mechanical red '{slug}' back for repeat "
                        f"{repeats} — the brief budget is spent; parked "
                        "for the cap ask",
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
                    detail = (
                        f"\n{_LAND_GATE_ERROR_HEADER}\n{line}"
                        if lint
                        else f" {line[:200]}"
                    )
                    w.comment(
                        n,
                        f"brief: {brief}{_templates.MECH_BRIEF_MARK}{last_job}:{detail}",
                    )
                mech_pinned = _mech_pin(plan, w, n, rungs, params, rules)
                res.notes.append(
                    f"td{n.id}: mechanical red '{slug}' (repeat "
                    f"{repeats}/{brief_cap}) — briefed, retry "
                    + (
                        "on the same rung"
                        if mech_pinned
                        else "climbs the ladder (last rung unpinnable)"
                    )
                )
        if job_type == DEFAULT_OP and not mech_pinned:
            go, climbed, esc_stamp = _escalate(
                w, plan, n, params, lane_table, rules, from_builder=repeated_lint
            )
            if not go:
                reason = climbed or "no eligible retry lane"
                res.dispatch_hold(n, reason.removeprefix(f"td{n.id}: "))
                continue
            if climbed:
                res.notes.append(climbed)
            if esc_stamp is not None:
                decision_stamp = esc_stamp
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
            # td543855, td573431, td573432). The first cut has none: it
            # pins to release.cut_host (gr713351 — a builder host that
            # claimed r333's cut had no deploy overlay), and with that
            # unset it is held, never left for any ssh_node host to claim.
            host = _release.pinned_host(w.store, n.id)
            if not host and job_type == "release_cut":
                host = _release.cut_host(w.store)
                if not host:
                    no_host = (
                        f"td{n.id}: release_cut held — release.cut_host is "
                        "unset, so the first cut has no host that can "
                        "deploy (gr713351). next: have a release operator run "
                        "`scripts/prod-precis settings set release.cut_host <node>` "
                        "with the deploy host's PRECIS_NODE."
                    )
                    _log_cut_refusal(w, res, n, no_host)
                    res.dispatch_hold(n, no_host.removeprefix(f"td{n.id}: "))
                    continue
            if host:
                params["target_node"] = host
        # gr741695: a hub is local to its host — the build, the review and
        # the land of a task whose work a hub holds run on that host.
        # Every run_slice / land_slice mint below this point carries it,
        # whichever path (first build, retry, re-arm, mechanical-red
        # retry, escalation, re-land) got here.
        hub_pin = _hub_pin(plan, w.store, n, job_type)
        if hub_pin is not None:
            pin_host, holder, pin_note = hub_pin
            if (override := _pin_job_host(params, pin_host, holder)) is not None:
                res.notes.append(f"td{n.id}: {override}")
            if pin_note:
                res.notes.append(f"td{n.id}: {pin_note}")
        spec = get_job_type(job_type)
        if spec is None:
            res.dispatch_hold(n, f"meta.op job_type {job_type!r} is not registered")
            continue
        problem = _params_problem(spec.params_schema, params)
        if problem is None and not w.dry_run and spec.validate_submit is not None:
            problem = spec.validate_submit(w.store, gripe_id=None, params=params)
        refusal = f"{job_type} not dispatched — {problem}" if problem else None
        if job_type == "release_cut":
            _log_cut_refusal(w, res, n, f"td{n.id}: {refusal}" if refusal else None)
        if refusal:
            res.dispatch_hold(n, refusal)
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
                meta_extra={
                    _dscore.SCORE_META_KEY: _claim_score(plan, n, job_type=job_type),
                    **(
                        {"decision": decision_stamp}
                        if decision_stamp is not None
                        else {}
                    ),
                    **(
                        {HUB_PIN_KEY: {"host": hub_pin[0], "todo": hub_pin[1]}}
                        if hub_pin is not None
                        else {}
                    ),
                    **({"conflict_try": conflict_try} if conflict_try else {}),
                    **(
                        {_templates.REVIEW_FALLBACK_KEY: review_fallback}
                        if review_fallback is not None
                        else {}
                    ),
                },
            )
            if conflict_try:
                w.stamp_meta(
                    n,
                    {
                        CONFLICT_TRIES_KEY: {
                            "n": conflict_try,
                            "job": job_id,
                            "sent_back": None,
                            "at": _iso(datetime.now(UTC)),
                        }
                    },
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

    _stamp_dispatch_holds(plan, w, res, now)


def _stamp_dispatch_holds(
    plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    if w.dry_run:
        return
    for n in plan.todos():
        reason = res.dispatch_holds.get(n.id)
        previous = n.meta.get(DISPATCH_HOLD_KEY)
        if reason is None:
            if previous is not None:
                w.stamp_meta(n, {DISPATCH_HOLD_KEY: None})
        elif not (
            isinstance(previous, dict)
            and previous.get("reason") == reason
            and previous.get("since")
        ):
            w.stamp_meta(n, {DISPATCH_HOLD_KEY: {"reason": reason, "since": _iso(now)}})


# ── (5) hygiene ────────────────────────────────────────────────────


def _job_chunk_text(store: Any, job_id: int, chunk_kind: str) -> str:
    """The newest ``chunk_kind`` chunk's text on the job."""
    try:
        chunks = [
            c
            for c in store.chunks.list_chunks_for_ref(job_id)
            if c.chunk_kind == chunk_kind and c.text
        ]
    except Exception:  # a missing tail must not stop the ask
        log.warning("plan_dispatch: reading job:%d chunks failed", job_id)
        return ""
    if not chunks:
        return ""
    return str(max(chunks, key=lambda c: (c.ord, c.id)).text).strip()


def _job_event_text(store: Any, job_id: int) -> str:
    """The newest ``job_event`` chunk's text (the job's failure reason)."""
    return _job_chunk_text(store, job_id, "job_event")


def _job_summary_text(store: Any, job_id: int) -> str:
    """The newest ``job_summary`` chunk's text — the run's own outcome
    account, written on success where ``job_event`` keeps only the
    submit's launch preamble."""
    return _job_chunk_text(store, job_id, "job_summary")


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

#: Options of the ask a land refused for capacity past
#: :data:`LAND_CAPACITY_WAIT_CAP` gets: keep waiting re-arms the wait, or stop.
_CAPACITY_ASK_OPTIONS: list[str] = [
    "keep waiting",
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
    capacity: str | None = None,
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
    elif capacity:
        # A land refused for capacity past LAND_CAPACITY_WAIT_CAP: the host
        # stayed full (not a re-land ask). The refused launch may be the
        # ship or a diagnostic probe after a red gate, so the ask says only
        # that the launch was blocked and leaves any gate result standing.
        wait = f"{int(LAND_CAPACITY_WAIT_CAP.total_seconds() // 60)} min"
        head = (
            f"job:{newest.id} {newest.status} — a land launch has been refused "
            f"for capacity for over {wait} ({capacity} at capacity)"
        )
        title = (
            f"td{n.id}: land launch blocked for {wait} — host {capacity} is at capacity"
        )
        options = list(_CAPACITY_ASK_OPTIONS)
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
    elif capacity:
        body = (
            f"The plan tick stopped re-minting the land of td{n.id} "
            f"({n.title.splitlines()[0] if n.title else ''}): {head}. "
            "The host could not admit the launch's footprint, and every land "
            "since the first refusal met the same full host; this says "
            "nothing about the gate, and any earlier gate result on the "
            "branch stands. Answer 'keep waiting' and "
            f"close this todo to let the tick keep re-minting td{n.id}'s "
            f"land for another {wait}; pick 'won't do' by closing "
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
    store: Any,
    n: _Node,
    j: _Node,
    minutes: int,
    reason: str,
    *,
    held_host: str | None,
) -> int:
    """Escalate a queued job's placement gap without blocking its claim.

    The node's :data:`_UNPLACED_ASK_KEY` dedups the ask, which is parked on
    the plan root like :func:`_mint_orphan_ask`."""
    job_type = str(j.meta.get("job_type") or "job")
    context = f"job jo{j.id} ({job_type}) queued {minutes}m — {reason} (td{n.id}, plan td{n.root})"
    title = f"td{n.id}: job jo{j.id} unplaceable {minutes}m — "
    body = f"td{n.id}'s job jo{j.id} ({job_type}) has sat queued {minutes} min. "
    if held_host is not None:
        options = ["raise the cap", "keep the hold", "drop the node"]
        title += f"raise the cap on {held_host}, keep the hold, or drop the node?"
        body += (
            f"The job is pinned to `{held_host}`, where the effective ssh_node cap is 0. "
            f"Set `claim.executor_cap.ssh_node.{held_host}` above 0 to lift the hold, "
            "then pick 'raise the cap' and close this todo done. "
            "Pick 'keep the hold' and close this todo done to acknowledge that "
            "the job will wait while the cap stays at 0. "
        )
    else:
        options = _UNPLACED_ASK_OPTIONS
        title += "provision a host, wait, or drop the node?"
        body += (
            f"The claim gate holds it because {reason}. To fix the gap, "
            "provision a host advertising the tokens (pick the first option and "
            "close this todo done). To keep waiting — the host comes back on its "
            "own — pick 'wait' and close this todo done. "
            "The sweeper's scheduler/unschedulable alert is the standing page meanwhile. "
        )
    body += (
        "To give up on the work, pick 'drop the node' "
        f"and close td{n.id} STATUS:won't-do (this ask then closes won't-do with it)."
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(options),
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    pin = j.meta.get(HUB_PIN_KEY)
    if isinstance(pin, dict) and pin.get("host"):
        body += (
            f"\n\nHub pin: the job is pinned to host `{pin['host']}` because "
            f"td{pin.get('todo')}'s work is in that host's hub "
            "(`meta.build_ref`), so no other host can run it — and a `start "
            "over` answer cannot help, its launch has to run on that host "
            "too. If you need to rebuild on another host, release the pin by "
            f"hand: cancel job jo{j.id} and clear `meta.build_ref` (and "
            "`meta.conflict` and `meta.conflict_tries`, and "
            f"`meta.pending_land`, if set) on td{pin.get('todo')}. That "
            "loses the work held on that host: the task builds again from "
            "the base."
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


def _unplaced_asked(plan: _Plan, n: _Node, j: _Node) -> bool:
    """``n`` already has the placement ask for job ``j``: the mark
    (:data:`_UNPLACED_ASK_KEY`) names this job, or its ask is still open —
    a closed ask is the ruling for the job it was raised on, never
    re-minted, but a later job of the node (a re-mint after the host
    gap changed) is its own question and asks again."""
    mark = n.meta.get(_UNPLACED_ASK_KEY)
    if not isinstance(mark, dict):
        return False
    ask_id = mark.get("ask")
    ask = plan.nodes.get(ask_id) if isinstance(ask_id, int) else None
    return mark.get("job") == j.id or (ask is not None and not _closed(ask))


def _zero_cap_hosts(conn: Any, hosts: set[str]) -> set[str]:
    """The ``hosts`` whose ``ssh_node`` lane cap is 0 (gr752561): the
    arming runbook's hold-at-zero suspends the lane, so a job pinned
    there never claims."""
    from precis.workers.executors._common import executor_lane_limits

    return {h for h in hosts if executor_lane_limits(conn, "ssh_node", h)[0] == 0}


def _step_unplaced(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """td543917: a note per plan job queued past :data:`QUEUED_NOTE_AFTER`
    whose placement capabilities (``ServiceSpec.placement``) no live host
    advertises together — the claim gate holds it until one does. The
    sweeper's ``scheduler/unschedulable:<id>`` alert pages for missing
    capabilities. A pin to a host held at cap 0 also produces a note and
    ask, even when that host advertises all required capabilities.

    gr657333: past :data:`QUEUED_ASK_AFTER` the node also files ONE ruling
    ask (:func:`_mint_unplaced_ask`), deduped per job on
    ``meta.unplaced_ask`` (:func:`_unplaced_asked`): the mark names the
    job, so a closed ask is the ruling for that job and is not re-minted
    for it, while a later job of the node that sits unplaced asks again
    (an open ask is never doubled). The heal check settles
    it: the node closes first → the ask closes ``won't-do``; the gap
    heals or the job leaves ``queued`` → ``done``.

    gr681494: the ask mints only when no dark host offers the job's whole
    token set either — a job whose only fit is a host the ``host-dark``
    alert already names waits on that one page instead of adding a
    per-job "provision a host?" ask to the flood. A job carrying a hub
    pin (:data:`HUB_PIN_KEY`) still asks when its host is dark: the work
    lives only there, and the ask says how to release the pin."""
    from precis.workers.executors._common import (
        advertised_by_host,
        dark_advertised_by_host,
        effective_placement,
        lane_adapter_map,
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
        dark = dark_advertised_by_host(conn)
        lane_map = lane_adapter_map(conn)
        held = _zero_cap_hosts(
            conn,
            {
                str(pin)
                for n in plan.todos()
                for j in plan.jobs(n)
                if j.status == "queued"
                and (pin := (j.meta.get("params") or {}).get("target_node"))
            },
        )

    def held_pin(j: _Node) -> str | None:
        pin = (j.meta.get("params") or {}).get("target_node")
        return str(pin) if pin and str(pin) in held else None

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
            and (held_pin(j) or placement_gap(j.meta, advertised, lane_map))
            for j in plan.jobs(n)
        )
        if still_blocked:
            continue
        w.comment(
            ask,
            f"plan_dispatch: td{n.id} no longer has a queued job with a "
            "placement gap — this ask is moot.",
        )
        w.set_status(ask, "done")
        res.notes.append(
            f"td{ask.id}: unplaced ask closed — td{n.id}'s placement gap healed"
        )
    for n, j in waiting:
        gap = placement_gap(j.meta, advertised, lane_map)
        held_host = None if gap else held_pin(j)
        if not held_host and not gap:
            continue
        minutes = int((now - j.created_at).total_seconds() // 60)
        reason = (
            f"pinned host {held_host} has ssh_node cap 0 "
            f"(host setting: claim.executor_cap.ssh_node.{held_host})"
            if held_host
            else f"no host advertises {{{', '.join(sorted(gap))}}}"
        )
        res.notes.append(f"td{n.id}: job jo{j.id} queued {minutes} min — {reason}")
        if (
            now - j.created_at <= QUEUED_ASK_AFTER
            or _unplaced_asked(plan, n, j)
            or _closed(n)
        ):
            continue
        # gr681494: a job whose whole token set fits one dark host waits
        # on that host's ``host-dark`` page — the one alert already names
        # the cause, so the per-job provision ask is noise. A ``target_node``
        # pin narrows the check to that host, as ``placement_gap`` does.
        # A hub pin (gr741695) is the exception: the task's work is only
        # in that host's hub, so the ask is where its release is spelled
        # out (:func:`_mint_unplaced_ask`), dark host or not.
        tokens = effective_placement(j.meta, advertised, lane_map)
        pin = (j.meta.get("params") or {}).get("target_node")
        waiting_on = (
            None
            if held_host or isinstance(j.meta.get(HUB_PIN_KEY), dict)
            else next(
                (
                    h
                    for h in ([str(pin)] if pin else sorted(dark))
                    if tokens <= dark.get(h, set())
                ),
                None,
            )
        )
        if waiting_on is not None:
            res.notes.append(
                f"td{n.id}: job jo{j.id} waiting on host-dark:{waiting_on}"
            )
            continue
        if w.dry_run:
            res.asks += 1
            res.notes.append(
                f"td{n.id}: would ask — job jo{j.id} unplaceable {minutes}m ({reason})"
            )
            continue
        ask_id = _mint_unplaced_ask(store, n, j, minutes, reason, held_host=held_host)
        w.stamp_meta(
            n,
            {
                _UNPLACED_ASK_KEY: {
                    "ask": ask_id,
                    "at": _iso(now),
                    "job": j.id,
                    "gap": sorted(gap),
                    **({"held_host": held_host} if held_host else {}),
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


def _review_miss(store: Any, n: _Node, last: _Node) -> str | None:
    """The verdict miss a reviewer's succeeded ``last`` attempt was
    judged on — ``meta.review``'s ``verdict: None`` on this job
    (``_step_reviews``'s stamp) — as a signature line, or ``None`` for a
    non-reviewer, a stamp on another job, or a verdict posted since the
    judging."""
    seen = n.meta.get("review")
    if not (
        _is_reviewer(n)
        and isinstance(seen, dict)
        and seen.get("job") == last.id
        and seen.get("verdict") is None
    ):
        return None
    verdict, raw = _read_verdict(store, n.id, last.created_at)
    if verdict is not None:
        # A verdict landed after the judging stamped the miss — never
        # sign a miss that no longer holds.
        return None
    what = (
        f"a malformed verdict {_first_line(raw)!r}" if raw else "no `review:` comment"
    )
    # ``left``, not ``succeeded with``: the miss line IS the signature
    # key, and ``succeeded``'s ``cceeded`` tail normalizes to ``<sha>``.
    return f"review job:{last.id} left {what}"


def _sampled_review(last: _Node) -> list[str]:
    """The quality-sampling reasons ``run_slice`` stamped on a succeeded
    attempt's ``meta.outcome.review`` (gr753725), or ``[]`` — an
    unsampled run, a job with no outcome record, or a failed attempt.
    A clean exit the sampling flagged is a deliverable miss the cheap
    half already marked for the expensive layer: the rescue owns its
    escalation."""
    outcome = last.meta.get("outcome")
    if not isinstance(outcome, dict):
        return []
    review = outcome.get("review")
    if not isinstance(review, dict) or not review.get("sampled"):
        return []
    return [str(r) for r in (review.get("reasons") or []) if str(r)]


def _judge_pending(store: Any, job_id: int) -> bool:
    """A live judge todo already covers ``job_id`` (``meta.judges.job`` —
    the run_quality pass's idempotency backlink)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND (meta->'judges'->>'job')::bigint = %s LIMIT 1",
            (int(job_id),),
        ).fetchone()
    return row is not None


def _escalate_sampled_miss(w: _Writer, n: _Node, last: _Node) -> tuple[str, int | None]:
    """Mint the run-quality judge for a clean deliverable miss the
    sampling flagged (gr753725): the judge is the automatic
    adjudication a suspiciously cheap completion needs — without this
    the flag stalls silently on the job row until a human notices.
    Idempotent on the run_quality pass's own markers
    (``outcome.review.minted``, the ``meta.judges`` backlink) and inert
    on a dry run or while the factory kill switch holds; a mint failure
    is a note, never a rescue failure. Returns ``(step suffix, judge
    root id)`` — the id only when this call minted it."""
    if n.meta.get("judges"):
        return " — judge tasks are exempt from quality escalation", None
    if w.dry_run:
        return " — its judge mints with the rescue", None
    if read_only_reason(w.store) is not None:
        return " — its judge mint held (factory off)", None
    outcome = last.meta.get("outcome")
    review = outcome.get("review") if isinstance(outcome, dict) else None
    if (isinstance(review, dict) and review.get("minted")) or _judge_pending(
        w.store, last.id
    ):
        return " — its judge already runs", None
    from precis.workers import run_quality as _run_quality

    try:
        root_id = _run_quality.mint_review(
            w.store, {"id": last.id, "meta": dict(last.meta)}
        )
    except Exception:
        log.warning("plan_dispatch: judge mint for td%d raised", n.id, exc_info=True)
        return " — its judge mint failed", None
    return f" — judge plan td{root_id} minted to adjudicate it", int(root_id)


def _rescue_node(
    plan: _Plan, w: _Writer, res: PassResult, n: _Node, cap: int, attempts: int
) -> None:
    """A ladder-exhausted ``run_slice`` node enters the rescue path
    (td584381 S1a) instead of the cap ask: ``enter_rescue`` tags it once,
    files or joins the signature gripe, and the rescue pass owns what
    follows — one diagnose-and-re-arm, then the rescue pass's own ask on
    a second exhaustion (``meta.rescue.n >= 1``), the end of the
    autonomous road. A clean exit the quality sampling flagged
    (``meta.outcome.review`` on the succeeded attempt) escalates in the
    same call: the run-quality judge plan mints for the attempt
    (gr753725), except when the task is itself a judge."""
    from precis.workers import factory_rescue as _rescue

    cls = "review" if _is_reviewer(n) else "builder"
    ended = _attempt_jobs(plan, n)
    last = ended[-1] if ended else plan.jobs(n)[-1]
    text = _job_event_text(w.store, last.id)
    if last.status == "succeeded":
        # A succeeded attempt records no failure — its newest
        # ``job_event`` is the submit's launch preamble, and signing
        # THAT lumps every same-lane silent outcome into one signature
        # gripe (gr685880: three no-verdict reviewers shared the launch
        # line). Sign the attempt's own account instead: a verdict-miss
        # reviewer's miss leads, its ``job_summary`` follows; the
        # summary alone for any other clean-exit class.
        miss = _review_miss(w.store, n, last)
        summary = _job_summary_text(w.store, last.id)
        if miss is not None:
            text = "\n".join(x for x in (miss, summary) if x)
        elif summary:
            text = summary
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # The signature key's error line is the failure's own first line —
    # skipping the ``run_slice:`` preamble and the ``--- log tail ---``
    # separator, else every same-class failure collapses into one
    # signature gripe. The preamble stays the fallback.
    error_line = next(
        (ln for ln in lines if not ln.lstrip().startswith(("run_slice:", "---"))),
        lines[0] if lines else None,
    )
    if (
        cls == "review"
        and last.status == "failed"
        and devin_retryable_agent_error(text) == "empty_response"
    ):
        error_line = _DEVIN_EMPTY_RESPONSE_KEY
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
    if last.status == "succeeded":
        # Say the cap's shape honestly: no lane failure drove it — the
        # newest attempt exited clean and left nothing to act on.
        step += (
            "; the newest attempt exited clean — a deliverable miss, not a lane failure"
        )
        sampled = _sampled_review(last)
        if sampled:
            # gr753725: a clean exit the quality sampling flagged is the
            # case the expensive layer exists for — the rescue mints its
            # judge, or the flag stalls silently on the job row.
            note, judge_root = _escalate_sampled_miss(w, n, last)
            step += (
                f"; the sampling flagged the clean exit ({', '.join(sampled)}){note}"
            )
            if judge_root is not None:
                res.notes.append(
                    f"td{n.id}: judge plan td{judge_root} minted for the "
                    "sampled clean exit"
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


def _tagged_age(record: Any, now: datetime) -> timedelta | None:
    """How long ago a ``meta.repo_release`` record (``state: tagged``) was
    tagged, else ``None`` (no record, not tagged, or no readable
    ``tagged_at``)."""
    if not isinstance(record, dict) or record.get("state") != "tagged":
        return None
    try:
        at = datetime.fromisoformat(str(record.get("tagged_at")))
    except ValueError:
        return None
    return now - (at if at.tzinfo else at.replace(tzinfo=UTC))


def _step_pin_stale(plan: _Plan, w: _Writer, res: PassResult, now: datetime) -> None:
    """td659522 step 4 F1: an OPEN pin-bump node whose one release node
    tagged over :data:`PIN_STALE_AFTER` ago and which has no live job raises
    ``pin_bump/stale-open:{todo}`` (warn; ``raise_alert`` dedups on the
    fingerprint). A succeeded ``pin_bump`` whose land never appeared is such a
    node: its newest job is terminal and nothing else runs. So is one whose
    live job is a parked gate image wait (td659522 F7) that has seen the
    image behind the pin for over :data:`PIN_STALE_AFTER`: the parked job
    would otherwise hide a node that may never close. The pass's live
    fingerprint set feeds :func:`resolve_stale_alerts`, so the alert
    resolves itself the tick after the node closes (its image wait saw the
    pin land) or the condition otherwise clears — a done node holds no
    queue slot (gr764155)."""
    live: list[str] = []
    for n in plan.todos():
        if _closed(n) or not _is_pin_bump(n):
            continue
        image = _pin_image(n)
        since = _pin_image_since(image) if image.get("ready") is not True else None
        waiting = since is not None and now - since > PIN_STALE_AFTER
        if plan.live_job(n) and not waiting:
            continue
        releases = _pin_releases(plan, n)
        if len(releases) != 1:
            continue
        rel = releases[0]
        age = _tagged_age(rel.meta.get("repo_release"), now)
        if age is None or age <= PIN_STALE_AFTER:
            continue
        live.append(f"stale-open:{n.id}")
        record = rel.meta["repo_release"]
        tag = str(record.get("tag") or "?")
        hours = int(age.total_seconds() // 3600)
        res.notes.append(
            f"td{n.id}: pin bump still open {hours} h after {tag} was tagged"
        )
        if w.dry_run:
            continue
        raise_alert(
            w.store,
            source=_PIN_STALE_SOURCE,
            fingerprint=f"stale-open:{n.id}",
            title=f"pin bump td{n.id} still open {hours} h after {tag} was tagged",
            detail=(
                (
                    f"pin-bump node td{n.id} has waited over "
                    f"{int(PIN_STALE_AFTER.total_seconds() // 3600)} h for the "
                    f"{PIN_GATE_IMAGE} gate image on {image.get('host')} "
                    f"({image.get('reason')}); "
                    if waiting
                    else f"pin-bump node td{n.id} is open with no live job; "
                )
                + f"release node td{rel.id} tagged {tag} {hours} h ago (limit "
                f"{int(PIN_STALE_AFTER.total_seconds() // 3600)} h). "
                + (
                    "Its image ask may have been closed without a rebuild."
                    if waiting
                    else "The bump failed, was never dispatched, or its land never ran."
                )
            ),
            severity="warn",
            subject_ref_id=n.id,
        )
    if w.dry_run:
        return
    resolved = resolve_stale_alerts(
        w.store, source=_PIN_STALE_SOURCE, live_fingerprints=live
    )
    if resolved:
        res.notes.append(f"{resolved} stale pin-bump alert(s) resolved")


def _todo_status_of(plan: _Plan, store: Any, ref_id: int) -> str | None:
    """``ref_id``'s STATUS: the loaded node's, else the store's tags."""
    node = plan.nodes.get(ref_id)
    if node is not None:
        return node.status
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _pin_image_since(image: dict[str, Any]) -> datetime | None:
    try:
        at = datetime.strptime(str(image.get("since")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return at.replace(tzinfo=UTC)


def _step_pin_image_ask(
    plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    """td659522 F7 (Reto 2026-11-15): an OPEN pin-bump node whose image wait
    found the land host's gate image behind the pin (``image.reason`` one
    of :data:`PIN_IMAGE_STALE`, or docker unreachable for over
    :data:`PIN_STALE_AFTER`) gets ONE coordinator ask naming
    ``scripts/build-image precis-dev``. The ask sits under the plan root and
    does not block the node: the glue is already blocked by the open node,
    and a block would stop the re-dispatch of a wait that ended without
    closing it. The factory never rebuilds the image; the wait closes the
    node and the ask on its first poll after the rebuild. Deduped on the
    node's :data:`PIN_IMAGE_ASK_KEY` while that ask is open for the same
    wanted sha; an ask closed while the image still lacks the pin is asked
    again, at most :data:`PIN_IMAGE_REASK_AFTER` after it was first seen
    closed (``closed_at`` on the mark). Backstop: an open image ask whose
    node is closed (by hand, or before the wait could close it) is closed
    with it, and the mark is cleared."""
    for n in plan.todos():
        if not _is_pin_bump(n):
            continue
        mark = n.meta.get(PIN_IMAGE_ASK_KEY)
        mark = mark if isinstance(mark, dict) else {}
        ask_id = mark.get("ask")
        if not isinstance(ask_id, int) or isinstance(ask_id, bool):
            ask_id = None
        ask_status = (
            _todo_status_of(plan, w.store, ask_id) if ask_id is not None else None
        )
        ask_open = ask_status is not None and ask_status not in TERMINAL_TODO_STATUSES
        if _closed(n):
            if ask_id is None:
                continue
            if ask_open:
                res.notes.append(f"td{n.id}: image ask td{ask_id} closed with its node")
                text = (
                    f"answer: td{n.id} is closed ({n.status}); the image wait is over"
                )
                ask = plan.nodes.get(ask_id)
                if ask is not None:
                    w.comment(ask, text)
                    w.set_status(ask, "done")
                elif not w.dry_run:
                    w.comment_id(ask_id, text)
                    w.store.add_tag(
                        ask_id,
                        Tag.closed("STATUS", "done"),
                        set_by="system",
                        replace_prefix=True,
                    )
            w.stamp_meta(n, {PIN_IMAGE_ASK_KEY: None})
            continue
        image = _pin_image(n)
        want, reason = image.get("want"), image.get("reason")
        if image.get("ready") is True or not isinstance(want, str) or not want:
            continue
        if reason == PIN_IMAGE_UNREACHABLE:
            since = _pin_image_since(image)
            if since is None or now - since <= PIN_STALE_AFTER:
                continue
        elif reason not in PIN_IMAGE_STALE:
            continue
        again: int | None = None
        if ask_id is not None and mark.get("want") == want:
            if ask_open:
                continue  # one open ask per wanted sha
            closed_at = _pin_image_since({"since": mark.get("closed_at")})
            if closed_at is None:
                # First pass that sees it closed: start the re-ask clock.
                w.stamp_meta(n, {PIN_IMAGE_ASK_KEY: {**mark, "closed_at": _iso(now)}})
                res.notes.append(
                    f"td{n.id}: image ask td{ask_id} closed, the image still "
                    "lacks the pin; asking again in "
                    f"{int(PIN_IMAGE_REASK_AFTER.total_seconds() // 60)} min"
                )
                continue
            if now - closed_at < PIN_IMAGE_REASK_AFTER:
                continue
            again = ask_id
        host = str(image.get("host") or "the land host")
        record = n.meta.get(PIN_RECORD_KEY) or {}
        landed = str(record.get("landed_sha") or "")
        package = str(record.get("package") or "the engine")
        have = str(image.get("have") or "no label")
        glue = sorted(
            bid
            for bid, blockers in plan.blockers.items()
            if any(b.id == n.id for b in blockers)
        )
        title = f"td{n.id}: rebuild the gate image on {host}"
        res.notes.append(f"td{n.id}: {title.split(': ', 1)[1]} ({reason})")
        if w.dry_run:
            res.asks += 1
            continue
        if reason == PIN_IMAGE_UNREACHABLE:
            seen = (
                f"docker on {host} has not answered `docker image inspect "
                f"{PIN_GATE_IMAGE}` for over "
                f"{int(PIN_STALE_AFTER.total_seconds() // 3600)} h, so td{n.id} "
                "cannot tell whether the gate image carries the pin."
            )
        else:
            seen = (
                f"The {PIN_GATE_IMAGE} gate image on {host} is {reason}: its "
                f"precis.autocatpath_rev label is {have[:12]}, the pin wants "
                f"{want[:9]}."
            )
        body = (
            f"td{n.id} moved precis's {package} pin"
            + (f" (landed as {landed[:9]})" if landed else "")
            + f". {seen} The factory never builds the image. On {host}, from a "
            f"precis checkout at or after {landed[:9] or 'that land'} (pull "
            f"first), run `scripts/build-image {PIN_GATE_IMAGE}`; td{n.id} "
            "closes itself (and this ask) within ~2 min of the rebuild."
            + (
                f" The previous ask td{again} was closed, but the image still "
                "does not carry the pin."
                if again is not None
                else ""
            )
            + (
                " Blocked until then: " + ", ".join(f"td{g}" for g in glue) + "."
                if glue
                else ""
            )
        )
        ask_id = _mint_coordinator_ask(
            w.store,
            plan,
            n,
            title=title,
            body=body,
            context=f"gate image {reason} on {host}: have {have[:12]}, want {want[:9]}",
            now=now,
            # Not a block: the open node already holds the glue, and a
            # blocked node could not re-dispatch a wait that ended early.
            block_node=False,
        )
        w.stamp_meta(n, {PIN_IMAGE_ASK_KEY: {"ask": ask_id, "want": want}})
        res.asks += 1


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
    _step_pin_stale(plan, w, res, now)
    _step_pin_image_ask(plan, w, res, now)
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
        spend_hit = (
            _spend_hit(plan, n, _spend_rungs(lane_table, n, params), rules)
            if rules and job_type == DEFAULT_OP
            else None
        )
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
        if not _capped(
            plan, n, cap, rungs=rungs, spend_capped=spend_hit is not None, now=now
        ):
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
        # A refusal's own ask (a human-only path, a model rule, the
        # coordinator route) wins over the conflict path's.
        conflict_files = (
            None
            if human or coord is not None or model_rule
            else _conflict_done(plan, n)
        )
        capacity_host = _capacity_wait_done(plan, n, now)
        lane_gap: str | None = None
        if (
            not human
            and not land_capped
            and capacity_host is None
            and mech_sig is None
            and coord is None
            and model_rule is None
            and free_sig is None
            and conflict_files is None
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
            and conflict_files is None
            and not human
            and not land_capped
            and capacity_host is None
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
                or conflict_files is not None
                else "file a capacity-wait ask"
                if capacity_host is not None and not land_capped
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
            # A stamped diversity refusal goes directly to the
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
        elif conflict_files is not None:
            # gr741695 (Reto's ruling gr735593): the land conflicts with the
            # base and the resolution attempts did not clear it. The ladder
            # is already spent where it can be, so the call is the
            # coordinator's.
            listed = ", ".join(f"`{f}`" for f in conflict_files[:12]) or "unknown"
            more = (
                f" and {len(conflict_files) - 12} more"
                if len(conflict_files) > 12
                else ""
            )
            unusable = _conflict_unusable(n)
            tried = (
                f"{CONFLICT_MAX_TRIES} conflict-only resolution attempts, the "
                "last on the strongest model, did not clear it"
                if unusable is None
                else "its conflict-only resolution attempts did not clear it, "
                f"and the last one could not run on the strongest model: {unusable}"
            )
            ask_id = _mint_coordinator_ask(
                w.store,
                plan,
                n,
                title=(
                    f"td{n.id}: land conflict not cleared by "
                    f"{CONFLICT_MAX_TRIES} attempts — a coordinator call"
                    if unusable is None
                    else f"td{n.id}: land conflict not cleared, strongest "
                    "model unusable — a coordinator call"
                ),
                body=(
                    f"The plan tick stopped dispatching td{n.id} "
                    f"({n.title.splitlines()[0] if n.title else ''}): its "
                    f"land conflicts with the base, and {tried}. "
                    f"Conflicted files: {listed}{more}. The work is in the "
                    "build host's hub (`meta.build_ref`); read the branch "
                    "against the base, resolve the conflict by hand or "
                    f"leave a `brief:` comment on td{n.id}, and close this "
                    f"ask `acknowledged` — td{n.id} gets a fresh set of "
                    "resolution attempts. Answer `start over` to rebuild "
                    "from the base, or close the todo STATUS:won't-do to "
                    "drop it."
                ),
                context=(
                    f"land conflict: {CONFLICT_MAX_TRIES} resolution "
                    f"attempts spent ({len(conflict_files)} file(s))"
                    if unusable is None
                    else f"land conflict: strongest rung unusable ({unusable})"
                ),
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
            # gr662077: the node's tail died on the same ``attempt_free``
            # reason — an infra wait no retry changes (a lane that never
            # frees, a land's ship folder held by a live git). Backoff
            # already gave the holder its window; finding and clearing
            # it is the coordinator's call. The ``blocked-by`` edge
            # re-arms the node on the answer.
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
                    f"the diff with attempt_free='{free_sig}' — an infra "
                    "wait that never cleared (gr662077). No ladder rung or "
                    "same-rung retry changes it: find and clear the "
                    "holder — a lane that never frees, a live git in the "
                    "land's ship folder, a dead rerun path — or drop the "
                    f"node. Closing this ask `acknowledged` re-arms "
                    f"td{n.id} with a fresh retry budget."
                ),
                context=f"attempt_free '{free_sig}' x{FREE_RUN_CAP} consecutive",
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
                capacity=capacity_host if not land_capped else None,
            )
        why = (
            "infra red routed to coordinator"
            if coord is not None
            else "different-model refusal"
            if model_rule is not None
            else "land conflict not cleared"
            if conflict_files is not None
            else f"mechanical red loop ({mech_sig})"
            if mech_sig
            else "human-only hit"
            if human
            else "re-land cap hit"
            if land_capped
            else f"land refused for capacity on {capacity_host}"
            if capacity_host is not None
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


# ── Park = save, resume anywhere (``_slice_park``) ────────────────


def _is_parked(plan: _Plan, n: _Node) -> bool:
    """A build parked for a human or the rescue pass: an open ask on it, a
    ``rescue:*`` or ``child-failed:*`` tag, or ``STATUS:paused``."""
    return bool(
        plan.open_asks(n)
        or _in_rescue(n)
        or n.status == "paused"
        or any(v.startswith("child-failed:") for v in n.open_tags)
    )


#: The job id an attempt ref ends in (``refs/factory/build/<todo>/<job>``,
#: ``_slice_ws.build_ref``).
_BUILD_REF_JOB_RE = re.compile(r"refs/factory/build/\d+/(\d+)$")

#: ``meta.push.state`` values whose work is in no hub, only in the attempt
#: folder on ``push.host`` (``_slice_ws.WITHHELD`` / ``FAILED``).
_FOLDER_ONLY_STATES = frozenset({"withheld", "failed"})

#: The ``meta.parked`` / ``meta.push`` fields a park record copies.
_PARK_FIELDS = ("host", "ref", "sha", "head", "wip", "state", "at")


def _folder_only(plan: _Plan, n: _Node) -> tuple[dict[str, Any], str] | None:
    """The work only an attempt folder holds: ``(record, note)`` from the
    newest terminal job's ``meta.push`` (a cancelled attempt included: a
    stopped job is the usual way a folder ends up holding work the hub
    lacks) when its state is ``withheld`` or ``failed`` (the folder ``slice-td<todo>-j<job>`` on ``push.host``), else
    ``None``. A newest push record in any other state says there is none;
    an older one is stale."""
    for j in sorted(plan.jobs(n), key=lambda j: j.id, reverse=True):
        push = j.meta.get("push")
        if j.status not in _JOB_TERMINAL or not isinstance(push, dict):
            continue
        if push.get("state") in _FOLDER_ONLY_STATES and push.get("host"):
            return {k: push.get(k) for k in _PARK_FIELDS} | {"job": j.id}, str(
                push.get("note") or ""
            )
        break
    return None


def _park_source(plan: _Plan, n: _Node) -> tuple[dict[str, Any], str] | None:
    """Where ``n``'s parkable work is: ``(parked record, note)``, or
    ``None`` when the task holds none. The record is ``{host, ref, sha,
    head, wip, state, at, job}``; the note is the finalize's reason for a
    folder-only state (empty otherwise).

    The task's ``meta.build_ref`` (work in its host's hub) wins; else the
    newest terminal job (a cancelled one included) whose ``meta.push`` is
    ``withheld`` or ``failed``
    (:func:`_folder_only`). When such a job is NEWER than the attempt
    ``build_ref`` names, the record also carries ``folder = {host, job,
    head, state, note}``: work a re-arm would not continue from, which the
    ask must name too. A newest push record in any other state parks
    nothing."""
    rec = n.meta.get(_templates.BUILD_REF_KEY)
    if (
        isinstance(rec, dict)
        and rec.get("host")
        and (rec.get("sha") or rec.get("head"))
    ):
        m = _BUILD_REF_JOB_RE.search(str(rec.get("ref") or ""))
        parked = {k: rec.get(k) for k in _PARK_FIELDS} | {
            "job": int(m.group(1)) if m else None
        }
        newer = _folder_only(plan, n)
        if newer is not None:
            fol, note = newer
            hub_at = _parse_ts(rec.get("at"))
            fol_at = _parse_ts(fol.get("at"))
            later = (
                fol["job"] > parked["job"]
                if parked["job"] is not None
                else bool(hub_at and fol_at and fol_at > hub_at)
            )
            if later:
                parked["folder"] = {
                    "host": fol.get("host"),
                    "job": fol["job"],
                    "head": fol.get("head"),
                    "state": fol.get("state"),
                    "note": note,
                }
        return parked, ""
    return _folder_only(plan, n)


def _park_head(parked: dict[str, Any]) -> str:
    """The 12-char commit a parked record names: the builder's last real
    commit, else what the ref holds."""
    return str(parked.get("head") or parked.get("sha") or "")[:12]


def _park_line(todo_id: int, parked: dict[str, Any], note: str = "") -> str:
    """The ask-body sentence naming where a parked build's work is: the
    host and the ref in that host's hub — or, for work no hub holds, the
    attempt folder and why — the head, and the two ways out."""
    host = parked.get("host")
    if parked.get("state") in _FOLDER_ONLY_STATES:
        why = f"{parked.get('state')}: {note}" if note else str(parked.get("state"))
        return (
            f"{_slice_park.BODY_MARK} host `{host}`, folder "
            f"`slice-td{todo_id}-j{parked.get('job')}`, head "
            f"`{_park_head(parked)}` — the work is only in that folder on "
            f"that host, not in its hub ({why}). Closing this ask re-arms "
            f"td{todo_id} from the newest work a hub holds, or the base: it "
            "does not read that folder, so recover the commits from it by "
            "hand first (closing also lifts STATUS:paused; a `waiting-for:*` "
            "hold stays until lifted by hand); answer `start over` to rebuild "
            "from the base."
        )
    wip = (
        " (the top commit is a `wip:` snapshot of the uncommitted tree)"
        if parked.get("wip")
        else ""
    )
    return (
        f"{_slice_park.BODY_MARK} host `{host}`, ref `{parked.get('ref')}` in "
        f"that host's hub, head `{_park_head(parked)}`{wip}. Closing this ask "
        f"re-arms td{todo_id} to continue from that commit on that host (and "
        "lifts STATUS:paused; a `waiting-for:*` hold stays until lifted by "
        "hand); answer `start over` to drop it and rebuild from the base."
    )


#: Start of the ask-body sentence naming newer work only a folder holds
#: (:func:`_park_newer_line`); its own mark, so an ask that already carries
#: the hub sentence still gets it when the folder-only job comes later.
NEWER_MARK = "Newer work, not in the hub:"


def _park_newer_line(todo_id: int, parked: dict[str, Any]) -> str | None:
    """The ask-body sentence for work newer than the hub's, held only by
    the attempt folder of a withheld or failed push — ``None`` when there
    is none: the folder and host, its head and why the hub lacks it, and
    that a re-arm continues from the hub commit, not from this."""
    fol = parked.get("folder")
    if not isinstance(fol, dict):
        return None
    why = (
        f"{fol.get('state')}: {fol.get('note')}"
        if fol.get("note")
        else fol.get("state")
    )
    head = str(fol.get("head") or "")[:12]
    return (
        f"{NEWER_MARK} host `{fol.get('host')}`, folder "
        f"`slice-td{todo_id}-j{fol.get('job')}`, head `{head}` ({why}). A "
        "re-arm continues from the hub commit above and does not read this "
        "folder: recover its commits by hand first, or they are lost."
    )


def _park_comment(todo_id: int, parked: dict[str, Any], note: str) -> str:
    """The ``parked:`` comment: host, ref (or folder) and commit."""
    job = parked.get("job")
    fol = parked.get("folder")
    if isinstance(fol, dict) and parked.get("state") not in _FOLDER_ONLY_STATES:
        return (
            f"parked: work in the hub of {parked['host']} at {_park_head(parked)} "
            f"(jo{job}; {parked.get('ref')}); newer work only in folder "
            f"slice-td{todo_id}-j{fol.get('job')} on {fol.get('host')} "
            f"({fol.get('state')}) — the hub does not hold it"
        )
    if parked.get("state") in _FOLDER_ONLY_STATES:
        why = f"{parked.get('state')}: {note}" if note else str(parked.get("state"))
        return (
            f"parked: work only in folder slice-td{todo_id}-j{job} on "
            f"{parked['host']} at {_park_head(parked)} (jo{job}; {why})"
        )
    return (
        f"parked: work in the hub of {parked['host']} at {_park_head(parked)} "
        f"(jo{job}; {parked.get('ref')})"
    )


def _step_park_stamp(plan: _Plan, w: _Writer, res: PassResult) -> None:
    """Whichever path parked a build — a hygiene ask, a rescue ask, a
    ``child-failed:`` bubble, ``STATUS:paused`` — copy where its work is
    onto it. The work is in the build host's hub (the task's
    ``meta.build_ref``, written by the worker after a push) or, when the
    push was withheld or failed, only in the attempt folder (the newest
    ended job's ``meta.push``); this step runs on the tick and so only
    reads those records (:func:`_park_source`): ``meta.parked = {host,
    ref, sha, head, wip, state, at, job}`` on the node, and the
    :data:`_slice_park.BODY_MARK` sentence — host, ref, head — in the
    body of every open ask blocking it. Idempotent: a stamp is rewritten
    only when the sha or head moves, an ask body only once."""
    for n in plan.todos():
        if _closed(n) or _resolve_op(n)[0] != DEFAULT_OP or not _is_parked(plan, n):
            continue
        source = _park_source(plan, n)
        if source is None:
            continue
        parked, note = source
        have = n.meta.get(_slice_park.PARKED_KEY)
        if not (
            isinstance(have, dict)
            and have.get("sha") == parked["sha"]
            and have.get("head") == parked["head"]
            and have.get("folder") == parked.get("folder")
        ):
            w.stamp_meta(n, {_slice_park.PARKED_KEY: parked})
            w.comment(n, _park_comment(n.id, parked, note))
            res.notes.append(
                f"td{n.id}: parked work on {parked['host']} at {_park_head(parked)}"
            )
        if w.dry_run:
            continue
        line = _park_line(n.id, parked, note)
        newer = _park_newer_line(n.id, parked)
        for ask in plan.open_asks(n):
            try:
                chunks = [
                    c
                    for c in w.store.chunks.list_chunks_for_ref(ask.id)
                    if c.chunk_kind == TODO_BODY_KIND
                ]
                body = str(chunks[0].text) if chunks else ""
                add = [
                    text
                    for mark, text in (
                        (_slice_park.BODY_MARK, line),
                        (NEWER_MARK, newer),
                    )
                    if text is not None and mark not in body
                ]
                if not add:
                    continue
                w.store.chunks.replace_body_chunk(
                    ask.id,
                    "\n\n".join([body, *add]).strip(),
                    chunk_kind=TODO_BODY_KIND,
                    source=NAME,
                    reason="park-save: where the parked work is",
                )
            except Exception:  # the stamp stands; the next pass retries
                log.warning(
                    "plan_dispatch: park note on ask td%d failed", ask.id, exc_info=True
                )


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


def _release_test_holds(plan: _Plan, w: _Writer, res: PassResult) -> _Plan:
    """Hold main's nightly fix plans and settle them on the merge receipt."""
    from precis.workers import release_test_fixes
    from precis.workers.job_types import land_slice

    state = release_test_fixes.ownership(w.store)
    if not (state.pending_all or state.pending or state.carried):
        return plan

    held: set[int] = set()
    for root in plan.roots:
        pending, fix = release_test_fixes.plan_disposition(w.store, root, state)
        if not (pending or fix):
            continue
        held.add(root)
        if fix is None:
            continue
        nodes = [n for n in plan.todos() if n.root == root]
        if any(plan.live_job(n) for n in nodes):
            continue
        if any(
            _resolve_op(n)[0] == DEFAULT_OP and _landed_sha(w.store, n.id)
            for n in nodes
        ):
            # This plan has its own land and verification tail.
            held.remove(root)
            continue
        if not w.dry_run:
            gid = land_slice._plan_gripe(w.store, f"plan:{root}")
            if gid is not None:
                land_slice._resolve_gripe(
                    w.store, gid, fix.head, fix.note, status="auto_verify"
                )
        for n in sorted(nodes, key=lambda n: n.id == root):
            pending_land = n.meta.get(_templates.PENDING_LAND_KEY)
            if _closed(n) and not pending_land:
                continue
            w.comment(n, f"carried: {fix.note}")
            if pending_land:
                w.stamp_meta(n, {_templates.PENDING_LAND_KEY: None})
            w.remove_tags(n, _claims(n))
            w.set_status(n, "done")
        res.notes.append(f"plan td{root}: {fix.note}")

    if not held:
        return plan
    return _Plan(
        [root for root in plan.roots if root not in held],
        {nid: n for nid, n in plan.nodes.items() if n.root not in held},
        plan.blockers,
    )


def enqueue_roots(store: Any, roots: list[int]) -> PassResult:
    """Queue doable work in freshly minted chains without waiting a tick.

    Share the tick lock and all dispatch guards; executor claimers own
    capacity and file-overlap admission.
    """
    res = PassResult(dry_run=False)
    if not roots:
        return res
    with try_xact_advisory_lock(
        store.dsn,
        TICK_LOCK_KEY,
        idle_timeout=TICK_LOCK_IDLE_TIMEOUT,
        application_name=TICK_LOCK_APP_NAME,
    ) as got:
        if not got:
            res.skipped = "another tick holds the lock"
            return res
        plan = _load(store, roots)
        w = _Writer(store, False)
        plan = _release_test_holds(plan, w, res)
        _step_dispatch(
            plan,
            w,
            res,
            _lanes.load_lanes(store),
            _spend_rules(store),
        )
    return res


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
        plan = _release_test_holds(plan, w, res)
        _step_dead_roots(store, plan, w, res)
        _step_gates(store, plan, w, res, now)
        _step_loops(store, plan, w, res, now)
        _step_reviews(store, plan, w, res, now, lane_table, rules)
        _step_todo_briefs(store, plan, w, res, now, lane_table, rules)
        _step_reopen(plan, w, res)
        _step_conflict_clear(plan, w, res)
        _step_rearm(plan, w, res)
        _step_verdicts(store, plan, w, res, now)
        _step_moot(store, plan, w, res)
        _step_phases(plan, w, res)
        _step_ci_rerun(store, plan, w, res, now)
        _step_brief_holds(store, plan, w, res, now)
        _step_dispatch(plan, w, res, lane_table, rules, now)
        _step_conflict_first(plan, w, res)
        _step_hygiene(plan, w, res, now, lane_table, rules)
        _step_park_stamp(plan, w, res)
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
    "live_and_held_nodes",
    "live_nodes",
    "load",
    "parse_verdict",
    "read_only_reason",
    "run_pass",
]
