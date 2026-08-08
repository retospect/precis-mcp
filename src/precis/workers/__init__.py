"""Background work: worker passes, scheduler cadences, and job executors.

Derived-queue core (ADR 0007)
-----------------------------
The worker's "queue" is the data itself: a chunk that has no row in
``chunk_embeddings`` for embedder ``bge-m3`` *needs* to be embedded.
No separate ``block_jobs`` / queue table — derived artifact tables
double as the work-tracking surface.

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
  ``refs`` (ADR 0017 sibling-worker shape): ``classify``, ``bib_parse``,
  ``bib_mark``, ``chase``, ``fetch``, ``hub_refine``, ``nursery``,
  ``sweeper``, ``heartbeat``, ``corpus_reconcile``, ``paper_reconcile``,
  ``openalex_enrich``, ``llm_summarize``, … (roster: ``registry.py``).
* **Executor passes** — drain ``kind='job'`` rows (:mod:`.executors`).
  The ``dispatch`` pass is the intent→compute bridge (ADR 0044): it walks
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
(``PRECIS_TOPICS_ENABLED``, ADR 0068). Two more knobs on the same table:
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
``structural`` / ``deep_review`` (env-gated eligibility, fleet-wide — a
wedged rotation on one host can't starve a review), ``dream_agent`` /
``anki_sync`` (host-pinned + ``eligible()``-gated; a pinned cadence
stalls while its host is down — that is the contract, §D staleness
alarms are the backstop). The lease IS the fleet-singleton throttle.

Review tiers
------------
Two SQL watchdog passes and two agentic reviewers, plus disk:

* ``nursery`` — SQL-only, every cycle: todo-tree incoherence + worker
  health, one ``kind='alert'`` per condition (detector catalogue +
  thresholds: ``nursery.py``). The ``critical`` categories
  (worker-restart, dead-worker, dispatch-stall, orphaned-coordinator,
  nas-denied, host-dark) page once via ``alerts.notify_critical_alert``
  on first sighting — a dead/stalled worker stalls the planner
  cluster-wide. Alerts replaced a per-minute memory digest that spun on
  its own churning fingerprint (>2000 near-dup memories/day).
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
  ``dream_agent`` has its own deny list keeping ``put`` + ``tag``.

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
* ``sweeper`` fails ``STATUS:running`` jobs older than
  ``PRECIS_STUCK_JOB_HOURS`` — except the three lease-owning executors,
  which self-heal via boot-epoch reclaim (see :mod:`.executors`);
  ``coordinator`` deliberately keeps this wall-clock backstop as its only
  crash recovery.
* ``corpus_reconcile`` (per-host ``pdf_locations`` presence ledger),
  ``paper_reconcile`` (standing dedup + hygiene heals), and
  ``openalex_enrich`` (abstract fill + card rebuild) each self-throttle
  via an ``app_state`` marker + a single-runner advisory lock.
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
chunk-tag cascade (ADR 0047) lives in ``classify.py``; the generic axis
runner in ``axis_pass.py``; the paper→topic cascade (ADR 0060) in
``classify_topics.py``.

Agentic dispatch
----------------
Reviewers, ``dream_agent``, and the executors route LLM work through the
switchable router (``utils/llm/``, ADR 0046): ``dispatch(LlmRequest)``
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
