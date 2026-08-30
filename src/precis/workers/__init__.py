"""Background work: worker passes, scheduler cadences, and job executors.

Derived-queue core
-----------------------------
The worker's "queue" is the data itself: a chunk with no row in
``chunk_embeddings`` for embedder ``bge-m3`` *needs* to be embedded. No
separate ``block_jobs``/queue table — derived artifact tables double as
the work-tracking surface. Ingest writes rows and returns; it never
enqueues or blocks on derived work (a worker outage delays embeddings,
never loses them — the missing row IS the queue entry).

Each :class:`WorkerHandler` owns one ``(output_table, model)`` pair:
:meth:`claim_batch` (``LEFT JOIN`` chunks against the output table, lock
rows ``FOR UPDATE OF c SKIP LOCKED``), :meth:`process` (pure — no DB, no
I/O), :meth:`write_ok`/:meth:`write_failed` (a failure marker means a
poison-pill chunk isn't re-claimed forever), :meth:`status` (``(total,
ok, failed, pending)`` for ``precis worker --status``/``precis health``).
:func:`run_handler_once` threads a batch through those four in one
transaction; :func:`run_loop` round-robins all registered handlers until
they claim zero rows, then sleeps and re-polls.

Pass taxonomy
-------------
Three pass shapes share ``run_loop``'s rotation (``runner.py``):

* **Chunk passes** — :class:`WorkerHandler` subclasses (``embed``,
  ``summarize``, ``chunk_keywords``).
* **Ref-passes** — self-contained claim/compute/write closures over
  ``refs`` (``classify``, ``bib_parse``, ``bib_mark``, ``chase``, ``fetch``,
  ``hub_refine``, ``nursery``, ``sweeper``, ``heartbeat``,
  ``corpus_reconcile``, ``paper_reconcile``, ``paper_meta_enrich``,
  ``openalex_enrich``, ``stub_rank``, ``paper_rank``, ``llm_summarize``,
  ``backlog_groom``, ``diagnose_scan``, … — roster: ``registry.py``).
* **Executor passes** — drain ``kind='job'`` rows (:mod:`.executors`). The
  ``dispatch`` pass is the intent→compute bridge: walks open todos with
  ``meta.executor`` and mints one child ``kind='job'`` per, stamping
  ``prio`` from the parent so urgency flows down the DAG.

``run_loop`` is strictly serial round-robin — one slow handler starves
every other pass. That drives the scheduler-lease cadences (below), the
dedicated GPU compute lane (``deploy/README.md``), and the heartbeat's own
daemon thread (``cli/worker.py`` starts ``start_heartbeat_thread`` so a
wedged rotation can't flap a false host-dark alert; the in-rotation
``heartbeat`` pass stays an idempotent, non-lease backstop — it's the
liveness signal the lease machinery is judged by). ``run_loop`` also
stamps :mod:`.activity` around each ref-pass call (``set_pass``/``clear``)
so a long, log-silent pass doesn't look like a dead worker; the snapshot
publishes into ``host_heartbeat.meta.activity``, rendered by the web
Status page's **Now** sub-tab.

Profiles + service registry
---------------------------
``--profile system`` (every node) / ``agent`` (the OAuth + MCP-config
gateway host: ``job_claude_inproc``, ``quota_check``) / ``all`` (their
union — the collapsed one-daemon-per-host deploy; topology in
``deploy/README.md``). Profile is pass *ownership*, not a live switch.
``registry.py`` is the declarative source of truth: one frozen
:class:`~precis.workers.registry.ServiceSpec` per pass/job-type/compute
service/daemon/serving endpoint. ``cli/worker.py`` derives its profile
sets via ``service_names_for_profile()``; the ``/env`` inspector reads
rows carrying an ``AgentIntrospect``; ``tests/test_worker_registry.py``
AST-parses ``cli/worker.py`` and fails CI on wiring/spec drift.

Run control — ``service_config`` is live, env is deploy-time only
-----------------------------------------------------------------
``service_config(host, service, prio, …)`` (``service_config.py``) is the
ONE live control surface: ``prio 0`` = off, ``1..10`` = claim weight.
Registration is structural only (``cli/worker.py::_should_register``:
profile membership, ``ServiceSpec.enable_env``, ``axis:`` prefix, or an
unknown plugin pass); the resolver reads live, per-cycle
(``run_loop``'s ``pass_gate``), never at registration — a prio flip
always has a registered pass to gate, no restart needed.
``PRECIS_*_ENABLED`` is retired as a live default: ``enable_env`` only
seeds the deploy-time row (``precis service seed``, INSERT-if-absent so
console overrides survive redeploys); a formerly dark-switched pass with no
row defaults OFF, and the no-row baseline also ANDs
``ServiceSpec.capability_env`` (all set non-empty on this host) — so
``--profile all`` can't default-on e.g. ``job_claude_inproc`` where
``PRECIS_MCP_CONFIG`` is absent. Per-item env seeds remain for
``axis:<id>``/``topic:<slug>``. Two more knobs on the same table:
``concurrency`` (in-pass thread-pool width, e.g. ``classify``'s per-row
cascade, hard-capped by ``PRECIS_CLASSIFY_MAX_CONCURRENCY``) and reserve
mode (a ``service='reserve'`` pseudo-row with ``expires_at``, checked
live inside heavy-executor claim transactions).

Scheduler cadences
------------------
``scheduler.py`` decentralizes recurring-work triggering — see its own
docstring for the lease mechanism. ``CADENCES``: ``cron_tick`` (60s),
``watch_poll`` (1h), ``health_digest`` (1h), ``materialize`` (300s),
``draft_refresh_scan`` (~4h — stalest-section pick over
``meta.draft_refresh``-opted drafts, one bounded ``draft_refresh`` job/draft;
clock = min ``created_at`` over live direct paragraphs), ``structural``/
``deep_review`` (dark-switched eligibility, fleet-wide — a wedged rotation on
one host can't starve a review), ``dream_agent``/``anki_sync``
(host-pinned + ``eligible()``-gated — a pinned cadence stalls while its
host is down; health_digest's staleness alarms are the backstop).

Review tiers
------------
Two SQL watchdog passes and two agentic reviewers, plus disk:

* ``nursery`` — SQL-only, every cycle, own docstring for the detector
  catalogue. ``critical`` categories (worker-restart, dead-worker,
  dispatch-stall, orphaned-coordinator, nas-denied, host-dark,
  embed-lane-stalled) page once via ``alerts.notify_critical_alert``.
* ``health_digest`` — the slow-rot sibling (hourly, SQL-only); own
  docstring for the check/route/push pipeline.
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
* ``embed`` is manual-only (``--only embed``); the ``materialize`` cadence
  mints bounded ``embed_batch`` jobs (default-ON,
  ``PRECIS_MATERIALIZE_EMBED=0`` opts out) above
  ``PRECIS_EMBED_BACKLOG_HIGH``, only when none are live — hysteresis
  coalesces churn into few large batches.
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
  ``PRECIS_RESTART_ONCE_ENABLED=1``.
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
* ``stub_rank`` — own docstring for the four-step S2-enrich/embed/rank/
  LLM-band pipeline; writes ``refs.prio`` (1=hottest..10=coldest), which
  ``fetch``'s claim query and the ``stubs``/``chase-queue`` backlog views
  sort on.
* ``paper_rank`` (console-gated, default-OFF) — own docstring; writes a
  query-independent 0-100 composite to ``refs.meta['paper_rank']``, never
  ``refs.prio``.
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
paper→topic cascade in ``classify_topics.py``.

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
