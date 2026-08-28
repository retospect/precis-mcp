"""Background work: worker passes, scheduler cadences, and job executors.

Derived-queue core
-----------------------------
The worker's "queue" is the data itself: a chunk that has no row in
``chunk_embeddings`` for embedder ``bge-m3`` *needs* to be embedded.
No separate ``block_jobs`` / queue table — derived artifact tables
double as the work-tracking surface. Ingest writes rows and returns;
it never enqueues or blocks on derived work (a worker outage delays
embeddings, never loses them — the missing row IS the queue entry).

Each :class:`WorkerHandler` owns one ``(output_table, model)`` pair
and exposes a uniform contract:

* :meth:`claim_batch` — ``LEFT JOIN`` chunks against the output
  table; lock chunk rows ``FOR UPDATE OF c SKIP LOCKED`` so
  concurrent workers don't double-process the same chunk.
* :meth:`process` — the actual computation (embed text, summarise,
  …). Pure: must not touch the DB.
* :meth:`write_ok` / :meth:`write_failed` — INSERT a result row,
  status ``'ok'`` or ``'failed'``. Failure marker rows mean a
  poison-pill chunk is *not* re-claimed forever.
* :meth:`status` — return ``(total, ok, failed, pending)`` for
  ``precis worker --status`` / ``precis health``.

The :func:`run_handler_once` orchestrator threads chunk rows through
those four methods in a single transaction; :func:`run_loop` polls
all registered handlers in round-robin until they return zero
claimed rows, then sleeps and re-polls.

Pass taxonomy
-------------
Three pass shapes share the one ``run_loop`` rotation (``runner.py``):

* **Chunk passes** — :class:`WorkerHandler` subclasses per the contract
  above (``embed``, ``summarize``, ``chunk_keywords``).
* **Ref-passes** — self-contained claim/compute/write closures over
  ``refs`` (sibling-worker shape): ``classify``, ``bib_parse``,
  ``bib_mark``, ``chase``, ``fetch``, ``hub_refine``, ``nursery``,
  ``sweeper``, ``heartbeat``, ``corpus_reconcile``, ``paper_reconcile``,
  ``paper_meta_enrich``, ``openalex_enrich``, ``stub_rank``, ``paper_rank``,
  ``llm_summarize``, ``backlog_groom``, ``diagnose_scan``, … (roster:
  ``registry.py``).
* **Executor passes** — drain ``kind='job'`` rows (:mod:`.executors`).
  The ``dispatch`` pass is the intent→compute bridge: it walks
  open todos with ``meta.executor`` and mints one child ``kind='job'``
  per, stamping ``prio`` from the parent so urgency flows down the DAG.

``run_loop`` is strictly serial round-robin — one slow handler starves
every other pass. That constraint drives three designs: the
scheduler-lease cadences (below), the dedicated GPU compute lane
(``deploy/README.md``), and the heartbeat's own daemon thread
(``cli/worker.py`` starts ``start_heartbeat_thread`` so a wedged rotation
can't flap a false host-dark alert; the in-rotation ``heartbeat`` pass
stays as an idempotent backstop, deliberately NOT lease-based — it is the
liveness signal the lease machinery is judged by).

``run_loop`` also stamps :mod:`.activity` around each ref-pass call
(``set_pass`` before, ``clear`` in a ``finally``) — a long, log-silent pass
(the 2026-08-09 ``fetch_oa`` monopolization) otherwise looks identical to a
dead worker from the outside. ``heartbeat.py`` publishes the snapshot into
``host_heartbeat.meta.activity``; the web Status page's **Now** sub-tab
renders it.

Profiles + service registry
---------------------------
``--profile system`` (every node) / ``agent`` (the OAuth + MCP-config
gateway host: ``job_claude_inproc``, ``quota_check``) / ``all`` (their
union — the collapsed one-daemon-per-host deploy; topology in
``deploy/README.md``). Profile is pass *ownership*, not a live switch.
``registry.py`` is the declarative source of truth: one frozen
:class:`~precis.workers.registry.ServiceSpec` per pass / job-type /
compute service / daemon / serving endpoint. ``cli/worker.py`` derives
its profile sets via ``service_names_for_profile()``; the ``/env``
inspector reads the rows carrying an ``AgentIntrospect``;
``tests/test_worker_registry.py`` AST-parses ``cli/worker.py`` and fails
CI when wiring and specs drift.

Run control — ``service_config`` is live, env is deploy-time only
-----------------------------------------------------------------
``service_config(host, service, prio, …)`` (``service_config.py``) is the
ONE live control surface: ``prio 0`` = off, ``1..10`` = claim weight.
Registration is purely structural (``cli/worker.py::_should_register``:
profile membership, ``ServiceSpec.enable_env`` present, ``axis:`` prefix,
or a plugin pass the registry doesn't know); the resolver is read only
per-cycle (``run_loop``'s ``pass_gate``), never at registration — so a
live prio flip always has a registered pass to gate, no restart needed.
``PRECIS_*_ENABLED`` is retired as a live default: ``enable_env`` only
seeds the deploy-time row (``precis service seed``, INSERT-if-absent so
console overrides survive redeploys); a formerly-env-gated pass with no
row defaults OFF. The no-row baseline also ANDs
``ServiceSpec.capability_env`` (all set non-empty on this host), so the
``--profile all`` union can't default-on e.g. ``job_claude_inproc`` where
``PRECIS_MCP_CONFIG`` is absent (gr193672). Per-item env seeds remain for
``axis:<id>`` (``PRECIS_AXES_ENABLED``) and ``topic:<slug>``
(``PRECIS_TOPICS_ENABLED``). Two more knobs on the same table:
``concurrency`` (in-pass thread-pool width for ``classify``'s per-row
cascade, hard-capped by ``PRECIS_CLASSIFY_MAX_CONCURRENCY``) and reserve
mode (a ``service='reserve'`` pseudo-row with ``expires_at``, checked
live inside heavy-executor claim transactions — see
``service_config.py``).

Scheduler cadences
------------------
``scheduler.py`` is the decentralized recurring-work trigger: every
worker runs the ``scheduler`` pass, claiming a due cadence is an atomic
conditional advance on ``scheduler_leases`` — exactly-once lives in
Postgres, not a singleton node. ``CADENCES``: ``cron_tick`` (60s),
``watch_poll`` (1h), ``health_digest`` (1h), ``materialize`` (300s),
``draft_refresh_scan`` (~4h — stalest-section pick over
``meta.draft_refresh``-opted drafts, mints one bounded ``draft_refresh``
job per draft; clock = min ``created_at`` over live direct paragraphs),
``structural`` / ``deep_review`` (env-gated eligibility, fleet-wide — a
wedged rotation on one host can't starve a review), ``dream_agent`` /
``anki_sync`` (host-pinned + ``eligible()``-gated; a pinned cadence
stalls while its host is down — that is the contract, §D staleness
alarms are the backstop; a registry cadence with no lease row at all is
flagged ``never-seeded`` by the same check). The lease IS the
fleet-singleton throttle.

Review tiers
------------
Two SQL watchdog passes and two agentic reviewers, plus disk:

* ``nursery`` — SQL-only, every cycle: todo-tree incoherence + worker
  health, one ``kind='alert'`` per condition (detector catalogue +
  thresholds: ``nursery.py``). The ``critical`` categories
  (worker-restart, dead-worker, dispatch-stall, orphaned-coordinator,
  nas-denied, host-dark, embed-lane-stalled) page once via
  ``alerts.notify_critical_alert`` on first sighting — a dead/stalled
  worker stalls the planner cluster-wide. Alerts replaced a per-minute
  memory digest that spun on its own churning fingerprint (>2000 near-dup
  memories/day).
* ``health_digest`` — the slow-rot sibling (hourly cadence, SQL-only):
  curated outcome checks (backlog ones idle-aware via
  ``precis.health_checks``), derived cadence staleness (every overdue
  ``scheduler_leases`` row, zero per-cadence config), and registry-derived
  coherence (an enabled ``ServiceSpec`` with zero ``worker_logs`` in 24h =
  "intended-on but silent" — a new pass needs no digest edits). Alerts
  under ``watchdog:<group>`` capped info/warn; a remediation router files
  one auto-closing gripe per condition past its self-heal budget; a daily
  all-green push is the dead-man's proof, plus an external ping
  (``PRECIS_DEADMAN_PING_URL``) for total-outage coverage.
* ``disk_check`` — SQL-free, every node: ``shutil.disk_usage`` over
  ``PRECIS_DISK_WATCH_PATHS``, warn/critical alerts (gr191008: a full
  data-node SSD stalled all prod writes).
* ``structural`` (5h dedup) / ``deep_review`` (144h) — opus reviewers via
  ``review.py``'s :class:`Reviewer` + ``run_review_pass`` driver (adding
  one is a ``Reviewer(...)`` instance). Driver-level guards: a failed
  dispatch writes a ``review-fail:<name>`` cooldown so an erroring pass
  backs off to ``min_interval_hours`` instead of re-dispatching every
  tick; ``_is_silent_empty`` converts a $0 / zero-tool-call / no-text
  "success" into a failure marker + ``review:empty:<name>`` alert
  (``tool_calls`` must be a definitive 0 — a cheap-but-real pass is never
  flagged). Reviewers pass explicit ``disallowed_tools`` (gr179501; these
  standing passes never set an envelope, so it defaults permissive);
  ``dream_agent`` has its own deny list keeping ``put`` + ``tag`` (its
  hypothesis proposals write their own motivation/provenance edges
  server-side, so the list stays tight without ``link``).

Notable pass mechanics
----------------------
* ``embed`` is manual-only (``--only embed``); the ``materialize``
  cadence mints bounded ``embed_batch`` jobs (default-ON,
  ``PRECIS_MATERIALIZE_EMBED=0`` opts out) above
  ``PRECIS_EMBED_BACKLOG_HIGH``, only when none are live — hysteresis
  coalesces churn into few large batches. The chunk queue stays derived,
  so an outage delays embeddings, never loses them.
* ``fetch`` / ``chase`` backoff are both exponential (the OA fetcher
  doubles per prior attempt; chase doubles ``WAITING_BACKOFF_MINUTES``
  per consecutive ``waiting``) — kills ``ref_events`` spin-loop floods.
  Explicit acquires pre-empt both queue rank and backoff:
  ``Store.pin_stub_for_fetch`` (the ``put(kind='paper')`` path) pins
  ``prio=1`` and its fresh ``oa_requeued`` stamp buys one immediate
  retry; auto-discovered stubs earn their turn via ``stub_rank``.
* ``sweeper`` fails ``STATUS:running`` jobs older than
  ``PRECIS_STUCK_JOB_HOURS`` — except the three lease-owning executors,
  which self-heal via boot-epoch reclaim (see :mod:`.executors`);
  ``coordinator`` deliberately keeps this wall-clock backstop as its only
  crash recovery. It also runs an ``unpark`` phase every pass: a parent
  latched behind a ``child-failed:*`` bubble (:mod:`precis.handlers._job_bubble`)
  gets up to ``UNPARK_CAP`` autonomous, cool-down-gated re-arms before
  latching the terminal ``child-failed-final`` tag.
* ``reaper`` (:mod:`.reaper`, run inside the sweeper pass) is the
  claim-registry epoch arm: one declarative row list over every claim
  type stamped with boot-epoch identity (``resource_slot_holds``,
  agentlogs) — the shared predicate is :mod:`precis.liveness`, extracted
  from the job-lease machinery, never forked. Reclaims the moment a
  holder's generation is provably replaced (deploy SIGKILL) instead of
  waiting out the slot-hold TTL — or, for zombie agentlogs, forever.
  Age-floored (``PRECIS_CLAIM_REAPER_MIN_AGE_S``) so a just-booted
  worker's claims survive the pre-first-heartbeat window; every action
  re-verifies non-liveness inside its own transaction. Design:
  ``docs/backlog/self-healing-spine.md`` Layer 1. Its flip side is the
  graceful drain: the worker's SIGTERM handler flips
  :func:`precis.liveness.request_drain`, which the streamed LLM client
  polls between SSE chunks and the OSS agent loop polls between turns —
  an in-flight call aborts with its partial salvaged (the
  ``StreamTimeout``/``paused`` retry path) instead of holding the unit
  into the stop-timeout SIGKILL that minted the orphans in the first
  place. External-HTTP retry/backoff sleeps honor it too (gr204611):
  :func:`precis.liveness.drain_sleep` wakes early on drain, and
  :func:`precis.utils.http.external_retry` (the shared S2/Crossref
  tenacity config) additionally refuses the next attempt once draining.
* ``conditions`` (:mod:`.conditions`, evaluated on ``health_digest``'s
  hourly lane) is the condition registry (spine Layer 2): declarative
  probe rows — pass-dead-on-host (a handler silent past budget on a
  live host: every registered pass logs a ``worker_logs`` row every
  cycle, so silence is death, not quiet), rescue-pass-cadence,
  pass-wedged (fresh heartbeat, stale ``meta.activity.since``),
  llm-degraded, dead-generation-claims — whose findings ride the
  digest's alert-sync/router unchanged. Its heal arm is
  :mod:`.bounded_heal` (the unpark shape as a shared primitive:
  attempts + exponential cooldown + cap + terminal latch + one
  cap-escalation gripe, state in ``app_settings`` with a CAS bump so
  concurrent evaluators can't double-fire); the one whitelisted action
  is **restart-once** (``cap=1``) — ssh + a sudoers grant scoped to
  exactly one worker-bounce command per platform (provisioned by
  ``redeploy-precis.yml``), dark until ``PRECIS_RESTART_ONCE_ENABLED=1``.
* ``doctor_tick`` (spine Layer 3, :mod:`.job_types.doctor_tick`) — the
  LLM judgment layer at the ``report`` dial, deliberately *outside* the
  detection path: an 8h scheduler cadence mints one idem-keyed
  ``claude_inproc`` job per window (mint is free; the spend is gated at
  dispatch like every agent job). The agent reads only published
  surfaces under the reviewer deny list — ``put(kind='gripe')`` is its
  sole write — and appends a four-section report (classification /
  diagnosis / what-was-healed / needs-a-human) to a per-UTC-day
  ``draft`` ref (:mod:`.doctor_report`, ``meta.author='doctor'``).
  ``health_digest``'s degraded/daily push swaps in the doctor's body
  when a report is fresh (:data:`.doctor_report.FRESH_WINDOW`, measured
  from the last appended tick, not draft creation) and falls back to
  the template on staleness, absence, or any lookup failure — the
  digest must send when the LLM is down, and the all-green heartbeat
  stays template-only (dead-man proof). The morning brief carries one
  doctor line, same degrade-to-empty posture. Design:
  ``docs/backlog/self-healing-spine.md`` Layer 3.
* ``corpus_reconcile`` (per-host ``pdf_locations`` presence ledger),
  ``paper_reconcile`` (standing dedup + hygiene heals), ``openalex_enrich``
  (abstract fill + card rebuild), and ``paper_meta_enrich`` (Crossref/
  OpenAlex author/entry_type/retraction re-resolve) each self-throttle
  via an ``app_state`` marker + a single-runner advisory lock.
* ``stub_rank`` S2-enriches + ``bge-m3``-embeds (into ``ref_embeddings``)
  every pending paper stub, then scores each against active-quest and
  recently-opened-paper anchor vectors to write ``refs.prio``
  (1=hottest..10=coldest); ``fetch``'s claim query and the
  ``stubs``/``chase-queue`` backlog views both sort on that prio, floating
  the relevant stubs instead of draining newest-first. Three-step pass —
  see :mod:`.stub_rank`'s own docstring.
* ``paper_rank`` (console-gated, default-OFF — its only enable is a
  ``service_config`` prio row, web Status → Services or ``precis service
  prio``) writes a query-independent 0-100 reading-priority composite to
  ``refs.meta['paper_rank']`` — never ``refs.prio``, which ``stub_rank``
  owns. See :mod:`.paper_rank`'s own docstring.
* ``cast_audio`` narrates the two daily casts (morning ``reading`` brief,
  evening ``nidra`` meditation) via a produce→narrate→publish spine;
  compose runs as ``claude_inproc`` job_types on ``Tier.BIG`` (an
  unattended daily deliverable must not sit on the fail-closed OAuth
  quota lane). Failed renders back off exponentially. ``card_forge`` is
  the morning card work (leech ladder + new cloze mint, observe-first
  autonomy default).

Bibliography + classifiers (pointers)
-------------------------------------
``bib_parse`` builds ``paper_bib_entries`` (parse + DOI match),
``bib_mark`` extracts inline ``[N]`` usage into ``chunk_citations``, and
``bib_retag`` is the manual, corpus-mutating remediation for mis-typed
bibliography chunks — each module's docstring is the record. The
chunk-tag cascade lives in ``classify.py``; the generic axis
runner in ``axis_pass.py``; the paper→topic cascade in
``classify_topics.py``.

Agentic dispatch
----------------
Reviewers, ``dream_agent``, and the executors route LLM work through the
switchable router (``utils/llm/``): ``dispatch(LlmRequest)``
over a transport registry where ``claude -p`` (``utils/claude_agent.py``)
is one adapter among peers — backend, per-tier model, and placement
chains are live-switchable via ``app_settings``. When
``PRECIS_AGENT_CONTAINER`` is set the same ``claude -p`` runs in a
throwaway container (:mod:`.executors.agent_container`), gated on a
verified capability probe and falling back in-process on infra failure.
"""

from precis.workers.base import (
    ArtifactStatus,
    ChunkRow,
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
    "ChunkRow",
    "EmbedHandler",
    "RakeLemmaHandler",
    "WorkerHandler",
    "run_handler_once",
    "run_loop",
]
