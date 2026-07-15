"""``precis worker`` — drive the derived-artifact queue (ADR 0007).

Run continuously to keep ``chunk_embeddings`` and ``chunk_summaries``
up-to-date as new chunks land. Two modes:

* ``precis worker`` — start the loop, processing batches forever.
  ``Ctrl-C`` exits cleanly between batches.
* ``precis worker --status`` — print one ``(total | ok | failed |
  pending)`` row per registered handler and exit. No work claimed.

By default both handlers run: ``embed:bge-m3`` and
``summarize:rake-lemma``. ``--only embed`` / ``--only summarize``
isolates one. For CI / tests, ``--embedder mock`` swaps the heavy
sentence-transformers model for the deterministic
:class:`precis.embedder.MockEmbedder` so the worker can be exercised
without downloading weights.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING, Literal

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.embedder import make_embedder
from precis.format import serialize
from precis.store import Store
from precis.workers import (
    EmbedHandler,
    RakeLemmaHandler,
    WorkerHandler,
    run_loop,
)

if TYPE_CHECKING:
    from precis.workers.runner import RefPass

# ── ref-pass scheduling priority ──────────────────────────────────
#
# ``run_loop`` (workers/runner.run_loop) walks ``ref_passes`` in list
# order, sequentially, once per cycle: a slow pass registered ahead of
# another delays every pass behind it by its full batch duration. That
# is how a post-outage ``fetch_oa`` backlog once froze the planner —
# the fetch pass monopolised the single worker thread for tens of
# minutes each cycle while ``dispatch`` (which mints the planner's
# ``plan_tick`` jobs) sat near the end of the registration list and
# never got a turn, so freshly-created ``LLM:*`` todos went
# un-dispatched cluster-wide.
#
# Fix: sort ``ref_passes`` by this band just before the loop so
# latency-critical *real work* (job execution + planner lifecycle)
# always runs before slow *background I/O* (paper/patent fetch, LLM
# enrichment, weekly reviewers). ``list.sort`` is STABLE, so the
# registration order is preserved within a band. Keyed by the
# closure's ``__name__``; anything unlisted falls in the DEFAULT band
# (after real work, before the heavy background tail). Applies to both
# profiles — on the agent worker it also keeps ``job_claude_inproc``
# (the plan_tick executor) ahead of the multi-minute opus reviewers.
_REF_PASS_PRIORITY_DEFAULT = 20
_REF_PASS_PRIORITY: dict[str, int] = {
    # 0 — job execution: the planner coroutine itself (plan_tick via
    # job_claude_inproc) plus the other job runners.
    "_job_claude_inproc_pass": 0,
    "_job_coordinator_pass": 0,
    "_job_ssh_node_pass": 0,
    "_job_claude_docker_pass": 0,
    "_wake_runner_pass": 0,
    # 10 — planner lifecycle: unblock, schedule, mint, unstick.
    "_auto_check_pass": 10,
    "_schedule_pass": 10,
    "_dispatch_pass": 10,
    "_sweeper_pass": 10,
    # 15 — cheap SQL health.
    "_nursery_pass": 15,
    # 20 — DEFAULT band (enrichment / indexing / reconcile / plugins).
    # 30 — heavy background tail: external fetch + LLM + reviewers.
    "_chase_pass": 30,
    "_fetch_pass": 30,
    "_gp_fetch_pass": 30,
    "_llm_summarize_pass": 30,
    "_classify_pass": 30,
    "_paper_glossary_pass": 30,
    "_structural_pass": 30,
    "_deep_review_pass": 30,
    "_dream_agent_pass": 30,
}


def _ref_pass_priority(fn: RefPass) -> int:
    """Scheduling band for a ref-pass closure (lower runs earlier)."""
    return _REF_PASS_PRIORITY.get(
        getattr(fn, "__name__", ""), _REF_PASS_PRIORITY_DEFAULT
    )


# Column order for ``precis worker --status``. Keeping it in one
# place means every renderer (TOON, JSON, table) sees the same
# shape, and adding a column lands in exactly one spot.
_STATUS_SCHEMA: list[str] = ["handler", "total", "ok", "failed", "pending"]

log = logging.getLogger(__name__)


HandlerKey = Literal[
    "embed",
    "summarize",
    "chunk_keywords",
    "chase",
    "fetch",
    "gp_fetch",
    "tag_embeddings",
    "job_claude_inproc",
]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def add_parser(sub: argparse._SubParsersAction) -> None:
    """Register the ``precis worker`` subcommand on ``sub``."""
    p = sub.add_parser(
        "worker",
        help="Drive the derived-artifact queue (embeddings, summaries).",
        description=(
            "Process chunks that lack a derived artifact (embedding or "
            "summary) and write the result back. Without a separate "
            "queue table — see ADR 0007 — the worker discovers work by "
            "LEFT JOIN-ing chunks against the output tables."
        ),
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Print one (total | ok | failed | pending) row per handler "
        "and exit. No work is claimed.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass (one batch per handler) and exit. "
        "Useful for smoke tests and ad-hoc backfills.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Chunks claimed per handler per pass (default 32). Larger "
        "batches amortise commit overhead but hold row locks longer.",
    )
    p.add_argument(
        "--idle-seconds",
        type=float,
        default=2.0,
        help="Sleep between passes when all handlers reported zero "
        "claimed rows (default 2.0).",
    )
    p.add_argument(
        "--profile",
        choices=("system", "agent"),
        default="system",
        help="Which pass rotation to run. 'system' (default) = the "
        "everything-except-heavy-LLM rotation: embed, summarize, "
        "chunk_keywords, chase, fetch, tag_embeddings, auto_check, "
        "schedule, nursery, dispatch, sweeper. 'agent' = "
        "the LLM-heavy rotation: dream_agent, structural, deep_review. "
        "Each of those gates itself via env (PRECIS_DREAM_AGENT=1, "
        "PRECIS_STRUCTURAL_REVIEW=1, PRECIS_DEEP_REVIEW=1) and via the "
        "PRECIS_LOAD_CEILING load-avg gate, so an agent profile worker "
        "that hits a tick with nothing to do exits in milliseconds. "
        "Slice-5 consolidation: deploy one LaunchDaemon per profile.",
    )
    p.add_argument(
        "--only",
        choices=(
            "embed",
            "summarize",
            "chunk_keywords",
            "chase",
            "fetch",
            "gp_fetch",
            "tag_embeddings",
            "job_claude_inproc",
            "dream_agent",
            "auto_check",
            "schedule",
            "nursery",
            "structural",
            "deep_review",
            "dispatch",
            "sweeper",
            "quota_check",
            "watch_poll",
            "news_poll",
            "briefing",
            "llm_summarize",
            "classify",
            "paper_glossary",
            "backlog_groom",
            "briefing_audio",
        ),
        default=None,
        help="Restrict to one handler kind. Overrides --profile when "
        "set. Useful for ad-hoc backfills (`--only embed --once`) and "
        "debugging.",
    )
    p.add_argument(
        "--with-llm",
        action="store_true",
        help="Enable the chase worker's LLM hooks (claude -p via "
        "precis.utils.claude_p) for multi-candidate disambiguation, "
        "chunk-localisation confirmation, and verifier-with-caveats. "
        "Default: deterministic chase only (no LLM cost). Also "
        "honoured via env PRECIS_CHASE_LLM=1.",
    )
    p.add_argument(
        "--fetch-inbox",
        default=None,
        help="Directory where the fetcher worker drops downloaded OA "
        "PDFs (default: PRECIS_WATCH_INBOX env, else "
        "~/work/new_papers/_oa_fetched). The watcher should be "
        "configured to scan this path so fetched PDFs land in the "
        "normal ingest flow.",
    )
    p.add_argument(
        "--unpaywall-email",
        default=None,
        help="Email to send as Unpaywall's required identification "
        "parameter (default: PRECIS_UNPAYWALL_EMAIL env). Without "
        "one, the fetch pass is skipped.",
    )
    p.add_argument(
        "--embedder",
        default=os.environ.get("PRECIS_EMBEDDER", "bge-m3"),
        help="Embedder name (default: PRECIS_EMBEDDER env, else "
        "'bge-m3'). Use 'mock' for tests / CI to skip the model "
        "download, or 'remote' to embed via a `precis serve-embeddings` "
        "service (set --embedder-url / PRECIS_EMBEDDER_URL).",
    )
    p.add_argument(
        "--embedder-url",
        default=os.environ.get("PRECIS_EMBEDDER_URL"),
        help="Endpoint(s) for --embedder remote (default: "
        "PRECIS_EMBEDDER_URL env). Ordered, comma-separated base URLs, "
        "e.g. http://127.0.0.1:8181. Ignored unless --embedder remote.",
    )
    p.add_argument(
        "--embedder-timeout",
        type=float,
        default=float(os.environ.get("PRECIS_EMBEDDER_TIMEOUT", "30.0")),
        help="Per-call HTTP deadline in seconds for --embedder remote "
        "(default: PRECIS_EMBEDDER_TIMEOUT env, else 30.0).",
    )
    p.add_argument(
        "--embedder-max-retries",
        type=int,
        default=int(os.environ.get("PRECIS_EMBEDDER_MAX_RETRIES", "3")),
        help="Max retries per endpoint for --embedder remote before "
        "falling back to the next (default: PRECIS_EMBEDDER_MAX_RETRIES "
        "env, else 3).",
    )
    p.add_argument(
        "--summarizer-model",
        default="rake-lemma",
        help="Summarizer model name as registered in the 'summarizers' "
        "table (default 'rake-lemma').",
    )
    p.add_argument(
        "--max-keywords",
        type=int,
        default=50,
        help="RAKE max_keywords (default 50). Honour the registered "
        "summarizer config if present.",
    )
    p.add_argument(
        "--min-phrase-words",
        type=int,
        default=1,
        help="RAKE min_phrase_words (default 1).",
    )
    p.add_argument(
        "--max-phrase-words",
        type=int,
        default=4,
        help="RAKE max_phrase_words (default 4).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    # ``--status`` is the only emit-tabular-data verb on this
    # subcommand; ``--format`` is meaningless for the run loop but
    # registering it on the worker parser keeps the flag visible in
    # ``precis worker --help`` so operators discover it without
    # hunting through ``--status`` alone.
    add_format_argument(p)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    """Top-level handler for ``precis worker``."""
    if args.batch_size <= 0:
        print("worker: --batch-size must be positive", file=sys.stderr)
        sys.exit(2)

    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    # Wire the secrets vault: bind the store so passes can vault.reveal(), and
    # scrub the DSN from env so the agent subprocesses this worker spawns
    # (plan_tick, sandbox, claude -p) don't inherit it (secrets-vault, ADR 0055).
    from precis import secrets as _secrets

    _secrets.adopt_process_store(store)
    # Bind the same store for the full LLM interaction log (route_log, migration
    # 0061) so every dispatch() call this worker makes is captured. Best-effort;
    # dark until bound.
    from precis import route_log as _route_log

    _route_log.bind_store(store)
    # Attach the centralised DB log handler now that we have a
    # working DSN. The file handler the worker's parent process
    # already set up stays in place as the bootstrap + fallback
    # channel (the DB handler degrades to it on flush failure).
    # Migration 0015 introduced worker_logs; older DBs that haven't
    # been migrated will fail INSERTs gracefully via the demote
    # path, so unattended deploys to a fresh DB don't die at boot.
    _attach_db_log_handler(dsn)
    _record_boot_event(store, profile=args.profile)
    try:
        handlers = _build_handlers(args, store)
        if args.status:
            _print_status(handlers, store, format=resolve_format(args))
            return

        # Slice-5 consolidation: passes group into two profiles. The
        # LaunchDaemon picks one via --profile=system|agent; --only
        # still overrides for ad-hoc backfills.
        #
        # Planner-coroutine slice (2026-06-15): ``job_claude_inproc``
        # moved off the system profile and onto the agent profile. The
        # runner shells out to ``claude -p`` with ``--mcp-config`` so
        # the in-process planner can call back via MCP; that requires
        # the hermes-owned ``~/.claude/mcp.json`` + OAuth state, which
        # only lives on the agent host (melchior). On data-host system
        # workers the runner used to claim plan_tick / fix_gripe jobs,
        # fail because PRECIS_MCP_CONFIG / OAuth was missing, and
        # bubble ``child-failed:<job>`` to the parent — a routing-
        # induced false negative. Moving the pass to the agent profile
        # restricts claims to the host that can actually execute.
        system_passes: frozenset[str] = frozenset(
            {
                "embed",
                "summarize",
                "chunk_keywords",
                "chase",
                "fetch",
                "gp_fetch",
                "tag_embeddings",
                "auto_check",
                "schedule",
                "nursery",
                "dispatch",
                "sweeper",
                # Coordinator passes ship on the system profile so every
                # cluster node can host long-running coordinator jobs
                # (precis-dft's dft_campaign). The wake_runner is cheap
                # (status-flip + audit chunk) and benefits from running
                # everywhere so wake latency stays low. The coordinator
                # itself does no compute on its own — it dispatches to
                # plugin job_types whose ``run`` decides what to do.
                "job_coordinator",
                "wake_runner",
                # ssh_node drains jobs that run on a remote node
                # (precis-dft's gpaw_relax → ssh spark docker run).
                # Ships on every node like the coordinator; the claim
                # lock (FOR UPDATE SKIP LOCKED) keeps a single worker
                # per job. See workers/executors/ssh_node.py.
                "job_ssh_node",
                # Hierarchical SOM cluster maps for the precis-web grid.
                # Self-gating: rebuilds one scope a day, idle otherwise
                # (PRECIS_CLUSTER_INTERVAL_HOURS), so it costs ~nothing
                # to host on every node.
                "clusterize",
                # Corpus-presence ledger: each node stats the held-paper
                # PDFs under its own PRECIS_CORPUS_DIR roots and records a
                # per-(sha,host) verdict, so the draft reader's "held but
                # missing" ▲ is a corpus-wide DB read, not a per-web-host FS
                # probe. Self-throttling (refresh window) → idle once fresh.
                "corpus_reconcile",
                # Paper-dedup reconcile: fold duplicate paper refs (shared
                # pdf_sha256 / DOI-modulo-case / id-less title-only stubs
                # that duplicate a held paper) into the survivor. Was
                # manual-only (`precis reconcile-duplicates`); this runs it
                # on a 24h cadence, single-runner (advisory lock) and
                # throttled via an app_state marker so it's one cheap read
                # between runs.
                "paper_reconcile",
            }
        )
        # dream_agent stays out of the profile — it has its own
        # cadence (15-min LaunchDaemon via dream-pass.sh) and gates
        # via PRECIS_DREAM_AGENT=1. The agent profile carries the
        # dedup-window reviewers (structural / deep_review) PLUS
        # ``job_claude_inproc`` (planner-coroutine slice).
        agent_passes: frozenset[str] = frozenset(
            {
                "structural",
                "deep_review",
                "job_claude_inproc",
                "quota_check",
            }
        )
        profile_passes = {
            "system": system_passes,
            "agent": agent_passes,
        }[args.profile]

        def _pass_enabled(name: str) -> bool:
            """True when this pass should run on this invocation.

            ``--only X`` wins over the profile when set (single-pass
            backfills). Otherwise the profile's pass set decides.
            """
            if args.only is not None:
                return args.only == name
            return name in profile_passes

        # Chunk-keybert pass (F20). Replaces the v1 segment_toc worker.
        # Runs after embeddings exist (the claim query requires
        # ``chunk_embeddings.status='ok'``). Default (no ``--only``)
        # runs the chunk-level handlers + this pass each cycle; the
        # ``--only chunk_keywords`` choice drops chunk-level work and
        # drains this queue alone.
        #
        # ``RefPass`` is imported at module scope under TYPE_CHECKING;
        # the annotation below is stringized (``from __future__ import
        # annotations``) so no runtime import is needed here.
        ref_passes: list[RefPass] = []
        if _pass_enabled("chunk_keywords"):
            from precis.workers.chunk_keywords import run_chunk_keywords_pass

            # Narrow to EmbedHandler so mypy sees the .embedder
            # attribute; the abstract WorkerHandler doesn't carry it.
            from precis.workers.embed import EmbedHandler
            from precis.workers.runner import BatchResult

            embed_handler = next(
                # ``isinstance`` alone suffices — every EmbedHandler's
                # name is ``embed:…``. Don't read ``h.name`` here: it
                # lazily round-trips to the embedder, and this runs at
                # worker boot when the embedder may not be up yet.
                (h for h in handlers if isinstance(h, EmbedHandler)),
                None,
            )
            kw_embedder = (
                embed_handler.embedder
                if embed_handler is not None
                else _resolve_embedder(args, store)
            )

            def _chunk_keywords_pass(batch_size: int) -> BatchResult:
                r = run_chunk_keywords_pass(store, kw_embedder, batch_size=batch_size)
                return BatchResult(
                    handler="chunk_keywords",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_chunk_keywords_pass)

        # Finding-chase pass — same sibling-worker pattern, but for
        # STATUS:tracing findings. Default-off LLM hooks via
        # --with-llm or PRECIS_CHASE_LLM=1. See ADR 0018 §"Worker"
        # for the sibling-vs-base-class rationale.
        if _pass_enabled("chase"):
            from precis.workers.chase import run_finding_chase_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _chase_pass(batch_size: int) -> _BatchResult:
                r = run_finding_chase_pass(
                    store, limit=batch_size, with_llm=args.with_llm
                )
                return _BatchResult(
                    handler="finding_chase",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_chase_pass)

        # Hierarchical SOM cluster maps (precis-web /clusters grid).
        # Time-gated full rebuild per scope; see workers/clusterize.py.
        if _pass_enabled("clusterize"):
            from precis.workers.clusterize import run_clusterize_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _clusterize_pass(batch_size: int) -> _BatchResult:
                r = run_clusterize_pass(store, batch_size=batch_size)
                return _BatchResult(
                    handler="clusterize",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_clusterize_pass)

        # Tag-embeddings pass — populates ``tag_embeddings`` so the
        # kind='tag' handler can serve semantic discovery
        # ("find tags related to carbon capture"). Idle most of the
        # time; one batched embed call per pass keeps cost flat.
        if _pass_enabled("tag_embeddings"):
            # Reuse the embed handler's embedder when available so we
            # don't double-load weights.
            from precis.workers.embed import EmbedHandler as _EmbedHandler
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.tag_embeddings import (
                run_tag_embeddings_pass,
            )

            embed_handler_te = next(
                # ``isinstance`` alone suffices; avoid ``h.name`` here —
                # it lazily round-trips to the embedder at worker boot.
                (h for h in handlers if isinstance(h, _EmbedHandler)),
                None,
            )
            te_embedder = (
                embed_handler_te.embedder
                if embed_handler_te is not None
                else _resolve_embedder(args, store)
            )

            def _tag_embeddings_pass(batch_size: int) -> _BatchResult:
                r = run_tag_embeddings_pass(store, te_embedder, batch_size=batch_size)
                return _BatchResult(
                    handler="tag_embeddings",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_tag_embeddings_pass)

        # job_claude_inproc — drains the `kind='job'` queue for jobs
        # whose meta.executor=='claude_inproc'. v1 only job_type is
        # fix_gripe; see precis-fix-gripe-help for the recipe.
        if _pass_enabled("job_claude_inproc"):
            from precis.workers.executors.claude_inproc import (
                run_claude_inproc_pass,
            )
            from precis.workers.runner import BatchResult as _BatchResult

            def _job_claude_inproc_pass(batch_size: int) -> _BatchResult:
                # Smaller cap than the default chunk batch — each job
                # runs a multi-minute LLM subprocess and we want the
                # outer loop to yield between attempts.
                r = run_claude_inproc_pass(store, limit=min(batch_size, 4))
                return _BatchResult(
                    handler="job_claude_inproc",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_job_claude_inproc_pass)

        # job_coordinator — drains the `kind='job'` queue for jobs
        # whose meta.executor=='coordinator'. These are long-running
        # orchestrators (precis-dft's dft_campaign is the first
        # consumer) that run one short slice per pass and yield
        # between phases. See workers/executors/coordinator.py.
        if _pass_enabled("job_coordinator"):
            from precis.workers.executors.coordinator import (
                run_coordinator_pass,
            )
            from precis.workers.runner import BatchResult as _BatchResult

            def _job_coordinator_pass(batch_size: int) -> _BatchResult:
                r = run_coordinator_pass(store, limit=min(batch_size, 4))
                return _BatchResult(
                    handler="job_coordinator",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_job_coordinator_pass)

        # job_ssh_node — drains the `kind='job'` queue for jobs whose
        # meta.executor=='ssh_node'. Each runs a plugin dispatch that
        # shells out to a remote node (precis-dft's gpaw_relax → ssh
        # spark docker run) and blocks until it finishes, so the cap is
        # small. See workers/executors/ssh_node.py.
        if _pass_enabled("job_ssh_node"):
            from precis.workers.executors.ssh_node import run_ssh_node_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _job_ssh_node_pass(batch_size: int) -> _BatchResult:
                r = run_ssh_node_pass(store, limit=min(batch_size, 2))
                return _BatchResult(
                    handler="job_ssh_node",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_job_ssh_node_pass)

        # job_claude_docker — drains sandbox_run jobs (meta.executor==
        # 'claude_docker') by launching a detached, cgroup-capped
        # container, polling it by name, and reaping it (ADR 0048 /
        # docs/design/sandbox-run.md). Registered **default-OFF** — only
        # under PRECIS_SANDBOX_ENABLED=1 (the sandbox hosts) or an
        # explicit `--only job_claude_docker`, mirroring classify. So a
        # deploy of this slice changes nothing until a human enables it
        # on a box with podman + a dedicated CLAUDE_CODE_OAUTH_TOKEN.
        if _pass_enabled("job_claude_docker") or os.environ.get(
            "PRECIS_SANDBOX_ENABLED"
        ):
            from precis.workers.executors.claude_docker import (
                run_claude_docker_pass,
            )
            from precis.workers.runner import BatchResult as _BatchResult

            def _job_claude_docker_pass(batch_size: int) -> _BatchResult:
                # Detached-poll: each tick is a cheap inspect + heartbeat
                # plus up to a couple of launches, so the cap is small.
                r = run_claude_docker_pass(store, limit=min(batch_size, 4))
                return _BatchResult(
                    handler="job_claude_docker",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_job_claude_docker_pass)

        # wake_runner — re-queues paused coordinator jobs whose wake
        # condition has fired (children done, time reached, ask-user
        # tag cleared, manual_kick tag added, or cancel_requested
        # overlay). Cheap status-flip + chunk write per re-queue;
        # no compute. See workers/wake_runner.py.
        if _pass_enabled("wake_runner"):
            from precis.workers.wake_runner import wake_pass_for_runner

            def _wake_runner_pass(batch_size: int) -> _BatchResult:
                return wake_pass_for_runner(store, batch_size)

            ref_passes.append(_wake_runner_pass)

        # llm_summarize — model-authored "very brief; some additional
        # detail" chunk summaries into chunk_summaries
        # (summarizer='llm-v1'), via the litellm `summarizer` alias.
        # Default-OFF: runs only via `--only llm_summarize` or
        # PRECIS_SUMMARIZE_LLM=1 — a 1M-chunk backfill is a deliberate,
        # node-targeted batch, not something every system worker should
        # pick up. See workers/llm_summarize.py.
        from precis.workers.llm_summarize import (
            SUMMARIZER_NAME,
            LlmClient,
            LlmConfig,
            run_llm_summarize_pass,
        )

        _summarize_cfg = LlmConfig.from_env()
        if _pass_enabled("llm_summarize") or _summarize_cfg.enabled:
            from precis.workers.runner import BatchResult as _BatchResult

            _summarize_client = LlmClient(_summarize_cfg)

            def _llm_summarize_pass(batch_size: int) -> _BatchResult:
                r = run_llm_summarize_pass(
                    store,
                    client=_summarize_client,
                    summarizer=SUMMARIZER_NAME,
                    batch_size=min(batch_size, 16),
                    concurrency=_summarize_cfg.concurrency,
                )
                return _BatchResult(
                    handler="llm_summarize",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_llm_summarize_pass)

        # classify — chunk-axis cascade (junk-gate -> role3), writing
        # ROLE3 chunk tags via the litellm `summarizer` alias. Default-OFF
        # (PRECIS_CLASSIFY_ENABLED=1 or --only classify): a 1.3M-chunk
        # backfill is a deliberate, node-targeted batch, like llm_summarize.
        # Forces model=`summarizer` (PRECIS_SUMMARIZE_MODEL=qwen returns
        # empty — it's a thinking model). See workers/classify.py +
        # scripts/classify/EVAL_RESULTS.md.
        if _pass_enabled("classify") or os.environ.get("PRECIS_CLASSIFY_ENABLED"):
            import dataclasses as _dc

            from precis.workers.runner import BatchResult as _ClsBatchResult

            _cls_cfg = _dc.replace(
                LlmConfig.from_env(),
                enabled=True,
                model=os.environ.get("PRECIS_CLASSIFY_MODEL") or "summarizer",
            )
            _cls_client = LlmClient(_cls_cfg)
            _cls_escalate = os.environ.get("PRECIS_CLASSIFY_ESCALATE_MODEL") or None

            def _classify_pass(batch_size: int) -> _ClsBatchResult:
                from precis.workers.classify import run_classify_pass

                r = run_classify_pass(
                    store,
                    client=_cls_client,
                    batch_size=min(batch_size, 16),
                    escalate_model=_cls_escalate,
                )
                return _ClsBatchResult(
                    handler="classify",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_classify_pass)

        # paper_glossary — per-paper inferred reading glossary (reading-prep
        # loop, slice 1). Harvests Schwartz-Hearst abbreviations + undefined
        # acronyms + KeyBERT keywords and makes ONE LLM call to cluster+define
        # them into an embeddable `card_glossary` chunk (ord=-1000). Derived /
        # idempotent / reversible; NO account writes. Default-OFF
        # (PRECIS_PAPER_GLOSSARY_ENABLED=1 or --only paper_glossary): a
        # corpus-wide backfill is a deliberate, node-targeted batch, like
        # classify. Model defaults to the cheap `summarizer` alias. See
        # workers/paper_glossary.py + docs/design/reading-prep-loop.md.
        if _pass_enabled("paper_glossary") or os.environ.get(
            "PRECIS_PAPER_GLOSSARY_ENABLED"
        ):
            import dataclasses as _pg_dc

            from precis.workers.runner import BatchResult as _PgBatchResult

            # A glossary is a multi-term JSON object, far larger than the
            # 220-token 2-part summary LlmConfig defaults to — that budget
            # truncates the JSON mid-object so it never parses (every paper
            # "fails"). Give it room; env-overridable.
            _pg_cfg = _pg_dc.replace(
                LlmConfig.from_env(),
                enabled=True,
                model=os.environ.get("PRECIS_PAPER_GLOSSARY_MODEL") or "summarizer",
                max_tokens=int(
                    os.environ.get("PRECIS_PAPER_GLOSSARY_MAX_TOKENS") or 2000
                ),
            )
            _pg_client = LlmClient(_pg_cfg)

            def _paper_glossary_pass(batch_size: int) -> _PgBatchResult:
                from precis.workers.paper_glossary import run_paper_glossary_pass

                r = run_paper_glossary_pass(
                    store,
                    client=_pg_client,
                    batch_size=min(batch_size, 8),
                )
                return _PgBatchResult(
                    handler="paper_glossary",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_paper_glossary_pass)

        # briefing_audio — narrate the morning news briefing onto the podcast
        # feed (the first automatic audio producer, docs/design/audio-feed.md).
        # Default-OFF (PRECIS_BRIEFING_AUDIO_ENABLED=1 or --only briefing_audio)
        # and TTS-host-only: it needs the `[tts]` extra + Kokoro model files +
        # ffmpeg + a PRECIS_PODCAST_DIR, all of which live on spark. Decoupled
        # from the briefing *job* (which runs on the agent worker, no TTS): this
        # pass reads the persisted `briefing-<date>` ref and self-schedules off
        # its existence, idempotent via a `meta.audio_episode_id` marker. See
        # workers/briefing_audio.py.
        if _pass_enabled("briefing_audio") or os.environ.get(
            "PRECIS_BRIEFING_AUDIO_ENABLED"
        ):
            from precis.workers.runner import BatchResult as _BaBatchResult

            _ba_image = os.environ.get("PRECIS_TTS_IMAGE")
            _ba_cmd = os.environ.get("PRECIS_TTS_CONTAINER_CMD") or "podman"
            _ba_podcast_dir = os.environ.get("PRECIS_PODCAST_DIR")
            _ba_voice = os.environ.get("PRECIS_BRIEFING_AUDIO_VOICE") or "af_heart"
            _ba_lang = os.environ.get("PRECIS_BRIEFING_AUDIO_LANG") or "en-us"
            _ba_scratch = os.environ.get("PRECIS_TTS_SCRATCH")

            if not _ba_image:
                # TTS runs in the precis-tts container (this worker venv has no
                # [tts] extra), so without an image there's no backend — don't
                # register a pass that would just fail every tick.
                log.warning(
                    "briefing_audio enabled but PRECIS_TTS_IMAGE unset — skipping "
                    "(needs the precis-tts container image)"
                )
            else:

                def _briefing_audio_pass(batch_size: int) -> _BaBatchResult:
                    from precis.workers.briefing_audio import (
                        has_pending_briefing,
                        run_briefing_audio,
                    )

                    # Cheap existence check first — an idle tick (one briefing a
                    # day) never launches a container.
                    if not has_pending_briefing(store):
                        return _BaBatchResult("briefing_audio", 0, 0, 0)

                    r = run_briefing_audio(
                        store,
                        image=_ba_image,
                        podcast_dir=_ba_podcast_dir,
                        voice=_ba_voice,
                        lang=_ba_lang,
                        container_cmd=_ba_cmd,
                        scratch_dir=_ba_scratch,
                    )
                    return _BaBatchResult(
                        handler="briefing_audio",
                        claimed=1 if r["published"] else 0,
                        ok=1 if r["published"] else 0,
                        failed=0 if r["published"] else 1,
                    )

                ref_passes.append(_briefing_audio_pass)

        # cast_audio — narrate the daily *casts* (morning reading-brief + evening
        # nidra) onto the podcast feed (docs/design/reading-prep-loop.md §Audio).
        # Same substrate as briefing_audio: TTS-host-only (spark), container-first,
        # self-scheduling off an un-narrated cast draft, idempotent via
        # meta.audio_episode_id. Default-OFF (PRECIS_CAST_AUDIO_ENABLED=1 or
        # --only cast_audio) + needs PRECIS_TTS_IMAGE. See workers/cast_audio.py.
        if _pass_enabled("cast_audio") or os.environ.get("PRECIS_CAST_AUDIO_ENABLED"):
            from precis.workers.runner import BatchResult as _CaBatchResult

            _ca_image = os.environ.get("PRECIS_TTS_IMAGE")
            _ca_cmd = os.environ.get("PRECIS_TTS_CONTAINER_CMD") or "podman"
            _ca_podcast_dir = os.environ.get("PRECIS_PODCAST_DIR")
            _ca_lang = os.environ.get("PRECIS_CAST_AUDIO_LANG") or "en-us"
            _ca_scratch = os.environ.get("PRECIS_TTS_SCRATCH")

            if not _ca_image:
                log.warning(
                    "cast_audio enabled but PRECIS_TTS_IMAGE unset — skipping "
                    "(needs the precis-tts container image)"
                )
            else:

                def _cast_audio_pass(batch_size: int) -> _CaBatchResult:
                    from precis.workers.cast_audio import (
                        has_pending_cast,
                        run_cast_audio,
                    )

                    # Cheap existence check first — an idle tick never launches a
                    # container.
                    if not has_pending_cast(store):
                        return _CaBatchResult("cast_audio", 0, 0, 0)

                    r = run_cast_audio(
                        store,
                        image=_ca_image,
                        podcast_dir=_ca_podcast_dir,
                        default_lang=_ca_lang,
                        container_cmd=_ca_cmd,
                        scratch_dir=_ca_scratch,
                    )
                    return _CaBatchResult(
                        handler="cast_audio",
                        claimed=1 if r["published"] else 0,
                        ok=1 if r["published"] else 0,
                        failed=0 if r["published"] else 1,
                    )

                ref_passes.append(_cast_audio_pass)

        # Backlog groomer (default-OFF; PRECIS_BACKLOG_GROOM_ENABLED=1 or
        # --only backlog_groom): promote open gripes into dispatchable
        # ``fix_gripe`` todos so the autonomous fixer substrate acts on the
        # bug backlog. Off by default because enabling it starts handing
        # repo bugs to claude_inproc — a deliberate flip, like classify.
        if _pass_enabled("backlog_groom") or os.environ.get(
            "PRECIS_BACKLOG_GROOM_ENABLED"
        ):
            from precis.workers.runner import BatchResult as _GroomBatchResult

            def _backlog_groom_pass(batch_size: int) -> _GroomBatchResult:
                from precis.workers.backlog_groom import run_backlog_groom_pass

                return run_backlog_groom_pass(store, batch_size=min(batch_size, 16))

            ref_passes.append(_backlog_groom_pass)

        # Plugin-registered ref passes: third-party packages can
        # ship their own background workers via the
        # ``precis.ref_passes`` entry-point group (precis-dft's
        # ``view_worker`` is the first consumer). Failure isolation
        # mirrors handler discovery — a broken plugin factory logs
        # a warning and the worker carries on with whatever did
        # register. The pass-name gate (``_pass_enabled``) still
        # applies so ``--only`` and the profile pass set honour
        # plugin passes the same way they honour built-ins.
        from precis.workers._plugin_passes import (
            discover_plugin_ref_passes,
        )

        for pass_name, plugin_callable, plugin_profiles in discover_plugin_ref_passes(
            store, profile=args.profile, args=args
        ):
            if not _pass_enabled(pass_name):
                continue
            if args.only is None and args.profile not in plugin_profiles:
                # Factory declared it doesn't belong on this profile.
                # ``--only`` overrides — when set, the factory has
                # already opted in regardless of profile.
                log.info(
                    "plugin ref pass %r declared profiles=%s but "
                    "running profile=%s; skipping",
                    pass_name,
                    sorted(plugin_profiles),
                    args.profile,
                )
                continue
            ref_passes.append(plugin_callable)
            log.info(
                "plugin ref pass %r registered (profile=%s)",
                pass_name,
                args.profile,
            )

        # Unpaywall OA fetcher — turns stub paper refs (DOI known,
        # pdf_sha256 IS NULL) into landed PDFs by checking Unpaywall
        # for an OA URL and downloading to the watch inbox. The
        # watcher's existing ingest path picks the file up and C7's
        # stub-upgrade promotes the row in place.
        if _pass_enabled("fetch"):
            from precis.workers.fetch_oa import run_oa_fetch_pass
            from precis.workers.runner import BatchResult as _BatchResult

            fetch_inbox = args.fetch_inbox  # may be None → worker uses env default
            fetch_email = args.unpaywall_email  # same

            def _fetch_pass(batch_size: int) -> _BatchResult:
                r = run_oa_fetch_pass(
                    store,
                    limit=batch_size,
                    inbox_dir=fetch_inbox,
                    email=fetch_email,
                )
                return _BatchResult(
                    handler="fetch_oa",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_fetch_pass)

        # Citation-forward watcher (docs/design/watching.md). Polls
        # Semantic Scholar for papers that cite our most-due salient
        # papers and mints metadata-only stubs; fetch_oa then OA-acquires
        # them. Deliberately NOT in system_passes/agent_passes — it makes
        # external S2 calls and must run on a cadence, not the hot loop.
        # Run it from a dedicated low-frequency cron via
        # ``precis worker --only watch_poll`` (mirrors the dream cron).
        if _pass_enabled("watch_poll"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.watch_poll import run_watch_pass

            def _watch_poll_pass(batch_size: int) -> _BatchResult:
                r = run_watch_pass(store, limit=batch_size)
                return _BatchResult(
                    handler="watch_poll",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_watch_poll_pass)

        # News ingestion (news kind). Walks the news_sources feed
        # registry, fetches + mints new articles as `news` refs. Like
        # watch_poll it makes external calls on a cadence, so it's
        # cron-driven via ``precis worker --only news_poll``, not in the
        # hot system/agent loop.
        if _pass_enabled("news_poll"):
            from precis.workers.news_poll import run_news_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _news_poll_pass(batch_size: int) -> _BatchResult:
                r = run_news_pass(store)
                return _BatchResult(
                    handler="news_poll",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_news_poll_pass)

        # Morning briefing — summarizes recent `news` refs into a dated
        # digest ref. LLM-backed (summarizer alias), so cron-driven via
        # ``precis worker --only briefing`` (e.g. a daily launchd tick),
        # never the hot loop.
        if _pass_enabled("briefing"):
            from precis.workers.briefing import run_briefing
            from precis.workers.runner import BatchResult as _BatchResult

            def _briefing_pass(batch_size: int) -> _BatchResult:
                r = run_briefing(store)
                return _BatchResult(
                    handler="briefing",
                    claimed=r["articles"],
                    ok=1 if r["ref_id"] is not None else 0,
                    failed=0,
                )

            ref_passes.append(_briefing_pass)

        # Google Patents fall-back fetcher — picks up patents OPS gave up
        # on (or is still 404-ing) and tries patents.google.com once.
        # Gated by PRECIS_GP_FETCH=1; the pass itself short-circuits when
        # the env isn't set so it's safe to include in the system profile
        # even on hosts that shouldn't run it. See ADR-pending /
        # docs/decisions about external-fetch goodwill.
        if _pass_enabled("gp_fetch"):
            from precis.workers.fetch_google_patents import run_gp_fetch_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _gp_fetch_pass(batch_size: int) -> _BatchResult:
                # Cap at 1 per pass — patents.google.com is a third-
                # party host and we want only one in-flight request at
                # a time per host. Combined with the env-gate being
                # set on only one host (see precis_shared_env), this
                # keeps the global rate at one request per pass cycle.
                # The exponential backoff inside the pass handles HTTP
                # transients without re-hammering.
                r = run_gp_fetch_pass(store, limit=min(batch_size, 1))
                return _BatchResult(
                    handler="fetch_google_patents",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_gp_fetch_pass)

        # Auto-check pass — drains the todo-tree's auto-task queue
        # (Slice 1b of todo-tree-plan.md). Cheap and SQL-only by
        # default — the registered evaluators are SQL queries, not
        # LLM calls — so it stays in the default cycle.
        if _pass_enabled("auto_check"):
            from precis.workers.auto_check import run_auto_check_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _auto_check_pass(batch_size: int) -> _BatchResult:
                return run_auto_check_pass(store, limit=batch_size)

            ref_passes.append(_auto_check_pass)

        # Schedule pass — Slice 4 of todo-tree-plan.md. Walks
        # level:recurring refs, mints subtasks for due ticks under
        # the Watches umbrella. SQL-only and idempotent
        # (meta.spawned_for_tick stamp), so it shares the default
        # cycle with auto_check.
        if _pass_enabled("schedule"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.schedule import run_schedule_pass

            def _schedule_pass(batch_size: int) -> _BatchResult:
                return run_schedule_pass(store, limit=batch_size)

            ref_passes.append(_schedule_pass)

        # Nursery pass — Slice 3 of todo-tree-plan.md. SQL-only
        # pattern matcher that surfaces local incoherence (orphans,
        # stale claims, long waits, stuck doable, stalled recurrings,
        # spin loops) as ``kind='alert'`` rows (one per condition,
        # deduped on fingerprint; cleared conditions auto-resolve).
        # Idempotent per pass — a still-present condition just bumps
        # its alert's seen_count, so the default rotation can include
        # this without spamming the table.
        if _pass_enabled("nursery"):
            from precis.workers.nursery import run_nursery_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _nursery_pass(batch_size: int) -> _BatchResult:
                return run_nursery_pass(store, limit=batch_size)

            ref_passes.append(_nursery_pass)

        # Structural review pass — Slice 3 of todo-tree-plan.md.
        # Opus-class semantic review of the tree's shape (drift
        # between outcomes and child actions, sibling
        # contradictions, depth/fanout warnings). Explicit-only:
        # NOT in the default rotation, since each pass is an
        # opus call. Gated by PRECIS_STRUCTURAL_REVIEW=1; the
        # Ansible role at cluster/roles/precis_structural sets
        # the env + fires the LaunchDaemon at 6h cadence.
        if _pass_enabled("structural"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.structural import run_structural_pass

            def _structural_pass(batch_size: int) -> _BatchResult:
                return run_structural_pass(store)

            ref_passes.append(_structural_pass)

        # Deep review pass — Slice 3 of todo-tree-plan.md. Weekly
        # full Allen-review. Explicit-only; gated by
        # PRECIS_DEEP_REVIEW=1. Same shape as structural with a
        # longer prompt, longer timeout, larger turn cap, and a
        # 6-day dedup window.
        if _pass_enabled("deep_review"):
            from precis.workers.deep_review import run_deep_review_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _deep_review_pass(batch_size: int) -> _BatchResult:
                return run_deep_review_pass(store)

            ref_passes.append(_deep_review_pass)

        # Dispatch pass — Slice 5 of todo-tree-plan.md. Walks open
        # todos with meta.executor set, mints kind='job' children
        # under them so the executor pool can run the work. SQL-only,
        # cheap, multi-host safe via FOR UPDATE SKIP LOCKED. Shares
        # the default rotation with auto_check + schedule + nursery.
        if _pass_enabled("dispatch"):
            from precis.workers.dispatch import run_dispatch_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _dispatch_pass(batch_size: int) -> _BatchResult:
                return run_dispatch_pass(store, limit=batch_size)

            ref_passes.append(_dispatch_pass)

        # Sweeper pass — recovers cascades after orphaned claims.
        # SQL-only: any kind='job' carrying STATUS:running older than
        # PRECIS_STUCK_JOB_HOURS (default 1h) is transitioned to
        # STATUS:failed with an `swept:claim-orphaned` tag, so the
        # parent todo's child-failed bubble lands and the planner can
        # re-tick. Multi-host safe via FOR UPDATE SKIP LOCKED.
        if _pass_enabled("sweeper"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.sweeper import run_sweeper_pass

            def _sweeper_pass(batch_size: int) -> _BatchResult:
                return run_sweeper_pass(store, limit=batch_size)

            ref_passes.append(_sweeper_pass)

        # Corpus-presence reconcile — maintain the per-host pdf_locations
        # ledger so the draft reader's held-but-missing ▲ is a corpus-wide
        # DB read, not a per-web-host FS probe. Each node stats the held
        # PDFs under its own PRECIS_CORPUS_DIR roots. Self-throttling via a
        # refresh window (idle once every verdict is fresh). No-op when this
        # node has no corpus roots configured.
        if _pass_enabled("corpus_reconcile"):
            from precis.corpus_layout import corpus_roots_from_env, host_name
            from precis.workers.corpus_reconcile import run_corpus_reconcile_pass
            from precis.workers.runner import BatchResult as _BatchResult

            _corpus_dirs = corpus_roots_from_env()
            _host = host_name()

            def _corpus_reconcile_pass(batch_size: int) -> _BatchResult:
                return run_corpus_reconcile_pass(
                    store, _corpus_dirs, _host, limit=batch_size
                )

            ref_passes.append(_corpus_reconcile_pass)

        # Paper-dedup reconcile — the standing sweep behind
        # `precis reconcile-duplicates`, now on a cadence. Merges
        # duplicate paper refs (pdf_sha256 / DOI-case / id-less title-only
        # stubs that duplicate a held paper) into the survivor. Cheap
        # between runs: a `paper_reconcile:last_run` app_state marker gates
        # the whole pass to once per PRECIS_PAPER_RECONCILE_REFRESH_HOURS
        # (default 24), and a single-runner advisory lock keeps just one
        # node sweeping corpus-wide.
        if _pass_enabled("paper_reconcile"):
            from precis.workers.paper_reconcile import run_paper_reconcile_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _paper_reconcile_pass(batch_size: int) -> _BatchResult:
                return run_paper_reconcile_pass(store, limit=None)

            ref_passes.append(_paper_reconcile_pass)

        # Quota-check pass — refresh the Claude.ai OAuth utilisation
        # snapshot via one 1-token `claude -p "quota" --output-format
        # json` call. Agent profile only: hermes's OAuth state lives
        # there. Short-circuits when the persisted snapshot is younger
        # than REFRESH_INTERVAL_S (default 600s), so the cost is one
        # SQL probe per idle cycle + a 2-token completion every 10 min.
        if _pass_enabled("quota_check"):
            from precis.workers.quota_check import run_quota_check_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _quota_check_pass(batch_size: int) -> _BatchResult:
                return run_quota_check_pass(store, limit=batch_size)

            ref_passes.append(_quota_check_pass)

        # dream_agent — replaces the legacy bash dream-pass.sh with
        # a Python-side dispatch through call_claude_agent. Loads the
        # directive prompt + soul + MCP config from env-pointed file
        # paths; same flag set as the bash script (no Web tools,
        # bypass permissions, 20 turns). Explicit-only; gated by
        # PRECIS_DREAM_AGENT=1. The cluster's precis_dream role
        # owns the file installation.
        if _pass_enabled("dream_agent"):
            from precis.workers.dream_agent import run_dream_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _dream_agent_pass(batch_size: int) -> _BatchResult:
                return run_dream_pass(store)

            ref_passes.append(_dream_agent_pass)

        # Real work before background I/O. The run loop is sequential
        # per cycle, so ordering is priority: job execution + planner
        # lifecycle must run ahead of slow fetch/enrichment/reviewer
        # passes or a fetch backlog starves ``dispatch`` and the
        # planner stalls. Stable sort ⇒ registration order preserved
        # within a band. See ``_REF_PASS_PRIORITY``.
        ref_passes.sort(key=_ref_pass_priority)

        stop_flag = _install_signal_handlers()
        run_loop(
            handlers,
            store,
            batch_size=args.batch_size,
            idle_seconds=args.idle_seconds,
            once=args.once,
            should_stop=lambda: stop_flag["stop"],
            ref_passes=ref_passes,
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_embedder(
    args: argparse.Namespace, store: Store | None = None
):  # -> Embedder
    """Build the embedder named by ``--embedder``, threading remote knobs.

    Routes ``--embedder-url`` / ``--embedder-timeout`` /
    ``--embedder-max-retries`` (env-defaulted in ``add_parser``) into
    :func:`precis.embedder.make_embedder` so ``--embedder remote`` reaches
    a ``precis serve-embeddings`` service. ``getattr`` defaults keep older
    call sites (and test Namespaces that omit the remote flags) working.

    When a ``store`` is supplied the corpus embedding dimension is passed
    as ``expected_dim`` so a wrong/upgraded remote model fails loudly at
    the boundary instead of writing incompatible vectors (ADR 0020).
    """
    return make_embedder(
        args.embedder,
        dim=store.embedding_dim() if store is not None else 1024,
        url=getattr(args, "embedder_url", None),
        timeout=getattr(args, "embedder_timeout", 30.0),
        max_retries=getattr(args, "embedder_max_retries", 3),
    )


def _build_handlers(
    args: argparse.Namespace, store: Store | None = None
) -> list[WorkerHandler]:
    """Materialise the handler list per ``--only`` / ``--profile`` flags.

    Embed / summarize handlers belong to the ``system`` profile; the
    ``agent`` profile is purely ref-pass driven (LLM reviewers + dream)
    and skips the heavy embedder load when it doesn't need it. Honour
    ``--only`` as the override for ad-hoc invocations.
    """
    handlers: list[WorkerHandler] = []
    profile = getattr(args, "profile", "system")
    is_system = profile == "system"

    def _want(name: str) -> bool:
        if args.only is not None:
            return args.only == name
        return is_system

    if _want("embed"):
        # MockEmbedder.dim defaults to 1024 to match the seeded
        # bge-m3 embedder column dim, so swapping it in for tests
        # does not require schema changes.
        embedder = _resolve_embedder(args, store)
        handlers.append(EmbedHandler(embedder))
    if _want("summarize"):
        handlers.append(
            RakeLemmaHandler(
                max_keywords=args.max_keywords,
                min_phrase_words=args.min_phrase_words,
                max_phrase_words=args.max_phrase_words,
                model_name=args.summarizer_model,
            )
        )
    return handlers


def _record_boot_event(store: Store, *, profile: str) -> None:
    """Write a single ``worker: started`` row to ``worker_logs``.

    This is the DB's only restart/boot signal — there was none, so a
    launchd/jetsam relaunch loop (the incident that orphaned plan_ticks
    for 1.5 days) was invisible: the post-restart log stream looks like
    steady state. The nursery's ``worker-restart`` detector counts these
    rows per ``(host, process)``.

    A **direct, synchronous INSERT** — deliberately NOT routed through the
    buffered :class:`BufferedDBLogHandler` — because that handler can drop
    the single startup record before its first successful flush (a
    size-driven flush during the boot log burst can demote the batch to
    the file channel before the DB connection warms; the boot line then
    shows up in the file log but never in ``worker_logs``). Best-effort: a
    failed insert must not block startup. A distinct human-readable line
    also goes to the file/stdout channel (different message text, so it is
    never double-counted even if it does reach the DB via the handler)."""
    from precis.utils.db_log_handler import _resolve_host_name, _resolve_process_name

    log.info("worker: starting (profile=%s pid=%d)", profile, os.getpid())
    try:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO worker_logs "
                "(host, process, level, logger, message, payload) "
                "VALUES (%s, %s, 'INFO', 'precis.cli.worker', "
                "'worker: started', %s::jsonb)",
                (
                    _resolve_host_name(),
                    _resolve_process_name(),
                    json.dumps(
                        {
                            "event": "boot",
                            "pid": os.getpid(),
                            "profile": profile,
                            # OS family (darwin/linux) so the nursery's
                            # worker-restart alert can tailor its diagnosis
                            # instead of guessing macOS/jetsam on a Linux host.
                            "platform": sys.platform,
                        }
                    ),
                ),
            )
            conn.commit()
    except Exception:
        log.warning("worker: failed to record boot event", exc_info=True)


def _attach_db_log_handler(dsn: str) -> None:
    """Attach the BufferedDBLogHandler to the root logger.

    Best-effort: a failure to construct the handler (bad DSN, table
    missing, network) shouldn't kill the worker — the file handler
    that systemd / launchd / docker piped stdout to keeps catching
    everything regardless.
    """
    try:
        from precis.utils.db_log_handler import BufferedDBLogHandler

        root = logging.getLogger()
        # Avoid double-attach when run() is called twice in the same
        # process (tests, signal-driven restarts).
        for existing in list(root.handlers):
            if isinstance(existing, BufferedDBLogHandler):
                return
        handler = BufferedDBLogHandler(dsn)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        # If the root logger's effective level is WARNING (Python
        # default), elevate to INFO so worker pass summaries land
        # in the table. Operators who want quieter logs can override
        # via PRECIS_LOG_LEVEL.
        env_level = os.environ.get("PRECIS_LOG_LEVEL", "INFO").upper()
        try:
            root.setLevel(getattr(logging, env_level))
        except AttributeError:
            root.setLevel(logging.INFO)
    except Exception:
        # The worker still works without DB logging; surface via
        # whatever handlers are already attached.
        logging.getLogger(__name__).exception(
            "failed to attach BufferedDBLogHandler — continuing without DB logs"
        )


def _print_status(
    handlers: list[WorkerHandler],
    store: Store,
    *,
    format: str = "toon",
) -> None:
    """Render one row per handler in *format* and print to stdout.

    The row schema is :data:`_STATUS_SCHEMA` — pinned in one place
    so TOON, JSON, and the ASCII table renderer all see the same
    column order. Defaulting to ``"toon"`` matches the pipe
    default chosen by :func:`resolve_format`; callers passing a
    TTY-bound process get ``"table"`` instead.

    The output is one document (header + N rows for tabular
    formats; a JSON array for ``"json"``); we deliberately do not
    emit a leading ``#`` comment any more — TOON's first line is
    the header, and ``awk -F'\\t' 'NR>1'`` works the same way.
    """
    rows: list[dict[str, object]] = []
    with store.pool.connection() as conn:
        for handler in handlers:
            status = handler.status(conn)
            rows.append(
                {
                    "handler": status.name,
                    "total": status.total,
                    "ok": status.ok,
                    "failed": status.failed,
                    "pending": status.pending,
                }
            )
    print(serialize(rows, format=format, schema=_STATUS_SCHEMA))


def _install_signal_handlers() -> dict[str, bool]:
    """Wire SIGINT/SIGTERM to a flag the loop polls between batches.

    A dict-of-bool — boring but works as a closure cell across the
    signal handlers and ``run_loop``'s ``should_stop`` callable
    without having to introduce a singleton or threading.Event.
    """
    flag = {"stop": False}

    def _handler(signum: int, _frame: object) -> None:
        log.info("worker: signal %d received; finishing batch", signum)
        flag["stop"] = True

    # SIGINT for interactive Ctrl-C; SIGTERM for systemd / docker
    # stop. We deliberately do NOT install SIGHUP — most operators
    # use it for "reload config" and we have no config to reload.
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return flag


__all__ = ["add_parser", "run"]
