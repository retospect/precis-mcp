"""Background work: worker passes, scheduler cadences, and job executors.

Derived-queue core
-----------------------------
Derived artifact tables are the queue. Chunks missing a ``bge-m3`` embedding
remain claimable through outages, without a separate ``block_jobs`` table.

Each :class:`WorkerHandler` owns one ``(output_table, model)`` pair.
:meth:`claim_batch` locks chunks with ``FOR UPDATE OF c SKIP LOCKED``.
:meth:`process` is pure. :meth:`write_ok`/:meth:`write_failed` persist results
or failures. :meth:`status` reports ``(total, ok, failed, pending)``.
:func:`run_handler_once` handles one batch. :func:`run_loop` rotates handlers
and polls when idle.

Preprint upgrade retries oldest checks first. Limiter refusals preserve retry eligibility.

Pass taxonomy
-------------
* **Chunk passes** subclass :class:`WorkerHandler`.
* **Ref-passes** claim, compute and write over ``refs``. :mod:`.ref_lease`
  manages ``claimed_by``. See ``registry.py`` and ``docs/runbooks/news-ops.md``.
* **Executor passes** drain ``kind='job'`` rows (:mod:`.executors`).
  ``dispatch`` mints jobs for open todos with ``meta.executor``, passing
  parent ``prio`` down the DAG. See ``docs/runbooks/minter-ops.md``.

Rescue clocks breaker waits from the first park because rechecks refresh
status (:mod:`.factory_rescue`). Queued jobs wait only where claim skips them. Mirror fetches avoid receive hooks
(:mod:`.factory_git_mirror`).
Gripe intake bounds frozen reports (``precis-work-help``). Todo sweeps
recheck under lock. Dispatch logs decisions.

Runner halt policy (:mod:`.halt_policy`, :mod:`.halt_retry`) parks infra
failures and gives task-design failures one repair attempt. :mod:`.halt_recovery`
keeps the infra breaker and cohort probe. :mod:`.halt_heal` locks
cadence halt state when applying a supervisor verdict.

``run_loop`` rotates serially. Scheduler leases, the GPU lane
(``deploy/README.md``), and threaded heartbeats isolate slow handlers.
The rotation heartbeat is an idempotent backstop. :mod:`.activity`
stamps ref passes in ``host_heartbeat.meta.activity`` (Status **Now**)
to distinguish silent work from worker death. Overdue rescue pauses Claude claims.

Job digests
-------------
:func:`run_loop` logs one :class:`.runner.BatchResult` per pass to
``worker_logs.payload`` (``precis logs --pass <name> --payload``). Counters
use ``extra``; blocked claims set ``report_only``. Crashes log under the
same ``payload.handler`` so the pass-dead probe sees them (gr328589).

Profiles + service registry
---------------------------
``--profile system`` (every node) / ``agent`` (OAuth + MCP-config gateway:
``job_claude_inproc``, ``quota_check``) / ``all`` (union, one daemon/host;
topology: ``deploy/README.md``). Profiles own passes, not live switches.
``registry.py`` declares one frozen
:class:`~precis.workers.registry.ServiceSpec` per pass/job-type/compute
service/daemon/serving endpoint. ``cli/worker.py`` derives its profile
sets via ``service_names_for_profile()``; the ``/env`` inspector reads
rows carrying an ``AgentIntrospect``; ``tests/test_worker_registry.py``
AST-parses ``cli/worker.py`` and fails CI on wiring/spec drift.

Run control — ``service_config`` is live, env is deploy-time only
-----------------------------------------------------------------
``service_config(host, service, prio, …)`` controls host placement
(``prio 0`` off, ``1..10`` claim weight). Registration is structural;
``run_loop`` resolves priority each cycle, so flips need no restart.
``PRECIS_*_ENABLED`` now seeds deploy-time rows only; INSERT-if-absent keeps
console overrides across redeploys. Missing rows default formerly gated
passes OFF, and also check ``ServiceSpec.capability_env`` so ``--profile all``
cannot enable ``job_claude_inproc`` without ``PRECIS_MCP_CONFIG``. Per-item
seeds remain for ``axis:<id>``/``topic:<slug>``. Same table controls in-pass
``concurrency`` (hard-capped for ``classify``) and expiring reserve mode,
checked inside heavy-executor claims.

Admission (gr647996): :func:`.admission.admit` atomically reserves footprints
over ``resource_slots``. :mod:`.heartbeat` reconciles running load; inherited
cgroups cannot establish service identity.

Scheduler cadences
------------------
``transcript_audit`` samples closed factory chains, dark by default;
:mod:`.transcript_audit`.

``memory_recall`` measures renders; :mod:`.memory_recall`.
``memory_recall_harvest`` queues proposals, default-OFF; :mod:`.memory_recall_harvest`.

:mod:`.retention` reports before soft-deleting unused news. Durable read
stamps survive receipt expiry. Citation filtering precedes the cap.

``scheduler.py`` decentralizes recurring-work triggering — see its own
docstring for the lease mechanism. ``CADENCES``: ``cron_tick`` (60s),
``watch_poll`` (1h), ``health_digest`` (1h), ``materialize`` (300s),
``draft_refresh_scan`` (~4h — stalest-section pick over
``meta.draft_refresh``-opted drafts (questless, or serving an active
quest), one bounded ``draft_refresh`` job/draft; clock = min
``created_at`` over live direct paragraphs), ``gripe_parents``
(300s — pipeline v2 dup-parent backfill: one ANN query per embedded gripe,
``meta.dup_parents`` marker write, ``meta.pipeline.claimed_at`` lease;
``gripe_parents.py``), ``structural``/
``deep_review`` (dark-switched eligibility, fleet-wide — a wedged rotation on
one host can't starve a review), ``dream_agent``/``anki_sync``
(host-pinned + ``eligible()``-gated — a pinned cadence stalls while its
host is down; health_digest's staleness alarms are the backstop).

``gripe_triage`` records why ask settlement needs durable metadata
and excludes dispatch priority nudges.

Review tiers
------------
Two SQL watchdog passes and two agentic reviewers, plus disk:

* ``nursery`` — SQL-only, every cycle, own docstring for the detector
  catalogue. ``critical`` categories (worker-restart, dead-worker,
  dispatch-stall, orphaned-coordinator, nas-denied, host-dark,
  embed-lane-stalled) page once via ``alerts.notify_critical_alert``.
  Alert-producer mechanics (``raise_alert``/``resolve_stale_alerts``):
  ``docs/runbooks/alert-ops.md``.
* ``health_digest`` — hourly SQL watchdog. Ops guide:
  ``docs/runbooks/health-digest-ops.md``.
  Taproot edge silence is unhealthy only with chase-eligible work overdue
  beyond 6h: the check mirrors tracing/acquiring eligibility, canonical-hub
  exclusion and waiting backoff. Eligibility age starts at queue entry,
  genuine advance or completed backoff expiry; failed/unknown activity
  cannot erase it (gr346342).
  MCP silence checks include completed gaps to catch recovery between runs.
* ``disk_check`` — SQL-free, every node: ``shutil.disk_usage`` over
  ``PRECIS_DISK_WATCH_PATHS``, warn/critical alerts.
* ``structural`` (5h dedup)/``deep_review`` (144h) — opus reviewers via
  ``review.py``'s :class:`Reviewer` + ``run_review_pass`` driver (adding
  one is a ``Reviewer(...)`` instance). A failed dispatch writes a
  ``review-fail:<name>`` cooldown (backs off to ``min_interval_hours``
  instead of re-dispatching every tick); ``_is_silent_empty`` converts a
  $0/zero-tool-call/no-text "success" into a failure marker +
  ``review:empty:<name>`` alert. Reviewers pass explicit
  ``disallowed_tools`` (these passes never set an envelope, so it
  defaults permissive); ``dream_agent`` has its own tighter deny list
  (keeps ``put``+``tag`` — its hypothesis proposals write their own
  motivation/provenance edges server-side).

Notable pass mechanics
----------------------
* ``anki_sync`` runs as a daily cadence and as an owner-bound on-demand job.
  Both use the per-account sync lock. The job needs a runner with a configured
  mirror and shares the cadence's AnkiWeb failure backoff. A successful cadence
  clears only the deadline it saw at start, preserving newer failures.
* ``chunk_keywords`` batches paper-local embedding outside DB connections
  (gr547587) and logs pass time and lifetime worker RSS (gr701709).
* ``embed`` is manual-only (``--only embed``); the ``materialize`` cadence
  mints bounded ``embed_batch`` jobs (default-ON,
  ``PRECIS_MATERIALIZE_EMBED=0`` opts out) above
  ``PRECIS_EMBED_BACKLOG_HIGH`` — or once the oldest pending chunk
  outwaits ``PRECIS_EMBED_BACKLOG_AGE_MINUTES`` — only when none are
  live: hysteresis coalesces churn into few large batches.
* ``fetch``/``chase`` backoff are both exponential (kills ``ref_events``
  spin-loop floods). ``Store.pin_stub_for_fetch`` (the
  ``put(kind='paper')`` path) pre-empts both queue rank and backoff:
  pins ``prio=1``, and its fresh ``oa_requeued`` stamp buys one immediate
  retry; auto-discovered stubs earn their turn via ``stub_rank``.
* ``sweeper`` — own docstring for the claim-orphan sweep + ``unpark``
  phase; ``coordinator`` deliberately keeps it as its only crash recovery.
* ``reaper`` (:mod:`.reaper`, run inside the sweeper pass) — own
  docstring for the claim-registry epoch-arm reclaim (spine Layer 1). Its
  flip side: the worker's SIGTERM handler flips
  :func:`precis.liveness.request_drain`, polled by the streamed LLM
  client and the OSS agent loop so an in-flight call aborts with its
  partial salvaged instead of holding into the SIGKILL that mints
  orphans; :func:`precis.liveness.drain_sleep`/
  :func:`precis.utils.http.external_retry` honor it too.
* ``conditions`` (:mod:`.conditions`, evaluated on ``health_digest``'s
  hourly lane) — own docstring for the probe catalogue (spine Layer 2).
  Its heal arm is :mod:`.bounded_heal` (attempts + exponential cooldown +
  cap + terminal latch + one cap-escalation gripe); the one whitelisted
  action is restart-once (``cap=1``), dark until
  ``PRECIS_RESTART_ONCE_ENABLED=1``. Fresh Linux heartbeats exclude inactive,
  disabled units from pass-dead, pass-wedged and heals. Crashed enabled units
  and missing or stale observations retain log expectations.
  Pass-dead uses registry and axis identities because run-control rows can
  outlive retired passes.
* ``doctor_tick`` (spine Layer 3, :mod:`.job_types.doctor_tick`) — own
  docstring for the judgment-layer design. ``health_digest``'s
  degraded/daily push swaps in the doctor's body when a report is fresh
  (:data:`.doctor_report.FRESH_WINDOW`) and falls back to the template on
  staleness/absence/lookup failure. The morning brief carries one doctor
  line, same degrade-to-empty posture.
* ``corpus_reconcile`` (per-host ``pdf_locations`` presence ledger),
  ``paper_reconcile`` (standing dedup + hygiene heals), ``openalex_enrich``
  (abstract fill + card rebuild), and ``paper_meta_enrich`` (Crossref/
  OpenAlex author/entry_type/retraction re-resolve) each self-throttle via
  an ``app_state`` marker + a single-runner advisory lock.
* ``orcid_enrich`` (tiers: :mod:`precis.utils.authors` docstring) — background
  ORCID identity tier: fetches unvisited ``kind='orcid'`` stubs,
  links held works, cross-checks each authored edge's paper DOI against
  the fresh record and verifies the matching ``paper_authors`` row
  (never rewrites the byline, gr732612). Self-throttles via
  ``app_state`` (no advisory lock — the ``meta.fetched_at IS NULL``
  claim predicate converges).
* ``stub_rank`` — own docstring for the four-step S2-enrich/embed/rank/
  LLM-band pipeline; writes ``refs.prio`` (1=hottest..10=coldest), which
  ``fetch``'s claim query and the ``stubs``/``chase-queue`` backlog views
  sort on.
* ``paper_rank`` (console-gated, default-OFF) — own docstring.
* ``cast_audio`` narrates the two daily casts (morning ``reading`` brief,
  evening ``nidra`` meditation) via a produce→narrate→publish spine;
  compose runs as ``claude_inproc`` job_types on ``Tier.BIG`` (an
  unattended daily deliverable must not sit on the fail-closed OAuth quota
  lane). Failed renders back off exponentially. ``card_forge`` is the
  morning card work (leech ladder + new cloze mint, observe-first
  autonomy default).

Bibliography + classifiers (pointers)
-------------------------------------
``bib_parse`` builds ``paper_bib_entries`` (parse + DOI match), ``bib_mark``
extracts inline ``[N]`` usage into ``chunk_citations``, and ``bib_retag``
is the manual, corpus-mutating remediation for mis-typed bibliography
chunks — each module's docstring is the record. The chunk-tag cascade
lives in ``classify.py``; the generic axis runner in ``axis_pass.py``; the
paper→topic cascade in ``classify_topics.py``. Its full-taxonomy marker
covers every enabled worker subset (the admin CLI keeps full traversal, so a
following worker pass must not churn the same papers); subset-only markers
stay sensitive to enabled-set changes so new topics backfill.

Agentic dispatch
----------------
Reviewers, ``dream_agent``, and the executors route LLM work through the
switchable router (``utils/llm/``): ``dispatch(LlmRequest)`` over a
transport registry where ``claude -p`` (``utils/claude_agent.py``) is one
adapter among peers — backend, per-tier model, and placement chains are
live-switchable via ``app_settings``. When ``PRECIS_AGENT_CONTAINER`` is
set the same ``claude -p`` runs in a throwaway container
(:mod:`.executors.agent_container`), gated on a verified capability probe
and falling back in-process on infra failure.
"""

from precis.workers.base import (
    ArtifactStatus,
    ClaimedChunk,
    WorkerHandler,
)
from precis.workers.embed import EmbedHandler
from precis.workers.runner import (
    BatchResult,
    run_handler_once,
    run_loop,
)
from precis.workers.summarize import RakeLemmaHandler

__all__ = [
    "ArtifactStatus",
    "BatchResult",
    "ClaimedChunk",
    "EmbedHandler",
    "RakeLemmaHandler",
    "WorkerHandler",
    "run_handler_once",
    "run_loop",
]
