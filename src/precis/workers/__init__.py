"""Background work: worker passes, scheduler cadences, and job executors.

Elsevier acquisition requires a structured XML body before success, including
a preflight for PDFs: HTTP 200 and PDF magic also admit abstract previews.
Missing body records an entitlement miss and continues the OA cascade; neither
short body length nor OA=false denies entitled full text. The explicit-cohort
``elsevier_abstract_backfill`` job preserves bodies/hashes/events and re-arms
existing fetch pins, with a dry-run default and no provider/model calls.

Derived-queue core
-----------------------------
The worker's "queue" is the data itself: a chunk with no row in
``chunk_embeddings`` for embedder ``bge-m3`` *needs* to be embedded. No
separate ``block_jobs``/queue table — derived artifact tables double as
the work-tracking surface. Ingest writes rows and returns; it never
enqueues or blocks on derived work (a worker outage delays embeddings,
never loses them — the missing row IS the queue entry).

Each :class:`WorkerHandler` owns one ``(output_table, model)`` pair.
:meth:`claim_batch` locks missing-output chunks with ``FOR UPDATE OF c SKIP
LOCKED``; :meth:`process` is pure, and :meth:`write_ok`/:meth:`write_failed`
persist results or poison-pill failures. :meth:`status` reports
``(total, ok, failed, pending)`` for the CLI/health view. One transaction in
:func:`run_handler_once` handles a batch; :func:`run_loop` rotates registered
handlers until idle, then sleeps and polls again.

Pass taxonomy
-------------
Three pass shapes share ``run_loop``'s rotation (``runner.py``):

* **Chunk passes** — :class:`WorkerHandler` subclasses (``embed``,
  ``summarize``, ``chunk_keywords``).
* **Ref-passes** — self-contained claim/compute/write closures over
  ``refs`` (``classify``, ``bib_parse``, ``bib_mark``, ``chase``, ``fetch``,
  ``hub_refine``, ``nursery``, ``sweeper``, ``heartbeat``,
  ``corpus_reconcile``, ``paper_reconcile``, ``paper_meta_enrich``,
  ``openalex_enrich``, ``orcid_enrich``, ``stub_rank``, ``paper_rank``,
  ``llm_summarize``, ``backlog_groom``, ``diagnose_scan``, ``news_poll``,
  ``briefing``, … —
  roster: ``registry.py``; ``news_poll``/``briefing`` dedup, backoff and
  delivery detail: ``docs/runbooks/news-ops.md``).
* **Executor passes** — drain ``kind='job'`` rows (:mod:`.executors`).
  ``dispatch`` bridges intent to compute: each open todo with
  ``meta.executor`` mints a child job inheriting parent ``prio`` so urgency
  flows down the DAG. Rejections and capabilities: ``docs/runbooks/minter-ops.md``.

``run_loop`` is serial round-robin, so one slow handler starves other passes.
This motivates scheduler leases, the separate GPU lane (``deploy/README.md``),
and the heartbeat daemon thread: a wedged rotation must not trigger a false
host-dark alert. The in-rotation heartbeat remains an idempotent non-lease
backstop, the liveness signal leases are judged by. :mod:`.activity` stamps
each ref pass so silent work is not mistaken for a dead worker; snapshots
appear in ``host_heartbeat.meta.activity`` and Status **Now**.

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
``service_config(host, service, prio, …)`` is the sole live control
(``prio 0`` off, ``1..10`` claim weight). Registration is structural;
``run_loop`` resolves priority each cycle, so flips need no restart.
``PRECIS_*_ENABLED`` now seeds deploy-time rows only; INSERT-if-absent keeps
console overrides across redeploys. Missing rows default formerly gated
passes OFF, and also check ``ServiceSpec.capability_env`` so ``--profile all``
cannot enable ``job_claude_inproc`` without ``PRECIS_MCP_CONFIG``. Per-item
seeds remain for ``axis:<id>``/``topic:<slug>``. Same table controls in-pass
``concurrency`` (hard-capped for ``classify``) and expiring reserve mode,
checked inside heavy-executor claims.

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
  Alert-producer mechanics (``raise_alert``/``resolve_stale_alerts``):
  ``docs/runbooks/alert-ops.md``.
* ``health_digest`` — the slow-rot sibling (hourly, SQL-only); own
  docstring for the check/route/push pipeline. Ops: ``docs/runbooks/
  health-digest-ops.md``.
  Taproot edge silence is unhealthy only with chase-eligible work overdue
  beyond6h: the check mirrors tracing/acquiring eligibility, canonical-hub
  exclusion and waiting backoff. Eligibility age starts at queue entry,
  genuine advance or completed backoff expiry; failed/unknown activity
  cannot erase that origin. Bursty output and empty queues must not
  masquerade as stalled producers (gr346342).
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
* ``orcid_enrich`` (tiers: :mod:`precis.utils.authors` docstring) — background
  ORCID identity tier: fetches unvisited ``kind='orcid'`` stub nodes,
  links held works, cross-checks each authored edge's paper DOI against
  the fresh record and verifies/overwrites the matching ``paper_authors``
  row. Self-throttles via an ``app_state`` marker only (no advisory lock —
  its ``meta.fetched_at IS NULL`` claim predicate already converges).
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
paper→topic cascade in ``classify_topics.py``. Its current full-taxonomy
marker covers every enabled worker subset: the admin CLI intentionally keeps
full traversal, and a following worker pass must not churn the same papers.
Subset-only markers stay sensitive to enabled-set changes so newly enabled
topics backfill those papers.

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
