"""``precis worker`` — drive the derived-artifact queue.

Run continuously to keep ``chunk_embeddings`` and ``chunk_summaries``
up-to-date as new chunks land. Two modes:

* ``precis worker`` — start the loop, processing batches forever.
  ``Ctrl-C`` exits cleanly between batches.
* ``precis worker --status`` — print one ``(total | ok | failed |
  pending)`` row per registered handler and exit. No work claimed.

By default ``summarize:rake-lemma`` runs. ``embed:bge-m3`` is
manual-only as of §F cycle b — the demand materializer's
``embed_batch``/``job_inproc`` path drains the embed queue in prod now
— so it only builds via an explicit ``--only embed`` (a one-off local
drain, or the ``PRECIS_MATERIALIZE_EMBED=0`` rollback). For CI / tests,
``--embedder mock`` swaps the heavy sentence-transformers model for the
deterministic :class:`precis.embedder.MockEmbedder` so the worker can be
exercised without downloading weights.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from precis import settings
from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.embedder import make_embedder
from precis.format import serialize
from precis.store import Store
from precis.utils.env import env_csv_set, env_flag
from precis.workers import (
    EmbedHandler,
    RakeLemmaHandler,
    WorkerHandler,
    run_loop,
)
from precis.workers.registry import (
    SERVICES_BY_NAME,
    service_names_for_profile,
)
from precis.workers.service_config import ServiceConfigResolver

if TYPE_CHECKING:
    from precis.workers.runner import RefPass


def _should_register(
    only: str | None, name: str, *, profile_passes: frozenset[str] = frozenset()
) -> bool:
    """Whether ``name`` registers into ``ref_passes`` on this invocation.

    §L control cutover: registration is now PURELY structural — never
    consults ``service_config`` (see the module's THE GAP note: a
    stale/absent row at boot used to keep a pass out of ``ref_passes``
    until restart, so a later live prio flip had nothing to gate). The
    per-cycle ``pass_gate`` (TTL-cached, consulted every cycle) is now
    the ONE decision point for whether a registered pass actually *runs*.

    ``only`` (``--only X``) forces exactly one pass regardless of
    profile membership. Otherwise always-register cases: a pass in this
    worker's profile rotation (``name in profile_passes`` — tied to
    ``--profile``, restart-time); a formerly-``PRECIS_*_ENABLED``-gated
    pass (registry's ``enable_env``); an ``axis:<id>`` pseudo-service; a
    name with NO ``ServiceSpec`` at all (a plugin factory's own pass —
    it already gated its own eligibility). A pass with neither profile
    membership nor ``enable_env`` does not register.
    """
    if only is not None:
        return only == name
    if name in profile_passes or name.startswith("axis:"):
        return True
    spec = SERVICES_BY_NAME.get(name)
    if spec is None:
        return True
    return bool(spec.enable_env)


def _capability_ok(name: str, environ: Mapping[str, str]) -> bool:
    """Whether this host satisfies ``name``'s ``ServiceSpec.capability_env``
    (every named var set non-empty) — vacuously true for a pass with none,
    or with no spec at all.

    gr193672: ``--profile all`` (§L-b collapsed worker) carries the
    ``_AGT``-only passes in every host's ``profile_passes``, so profile
    membership alone defaulted ``job_claude_inproc`` / ``quota_check`` ON
    fleet-wide and claude plan ticks hard-failed wherever the claude CLI /
    MCP config is absent. This feeds ``_profile_default_on``'s no-row
    baseline only; an explicit ``service_config`` row still overrides in
    either direction (the §L control surface is unchanged).
    """
    spec = SERVICES_BY_NAME.get(name)
    if spec is None or not spec.capability_env:
        return True
    return all(environ.get(var) for var in spec.capability_env)


def _axis_id_default_on(service: str, axes_env: frozenset[str]) -> bool | None:
    """Env default for an ``axis:<id>`` service — whether its id is listed in
    ``PRECIS_AXES_ENABLED`` (``axes_env``).

    Returns ``None`` for a non-axis service (the caller falls back to the
    registry/profile default). An ``axis:<id>`` service has no ``ServiceSpec`` of
    its own — the spec is named ``axis`` — so its default can't come from
    ``SERVICES_BY_NAME``; this is where the per-axis env seed is read for the
    per-cycle gate.
    """
    prefix = "axis:"
    if service.startswith(prefix):
        return service[len(prefix) :] in axes_env
    return None


def _topic_slug_default_on(service: str, topics_env: frozenset[str]) -> bool | None:
    """Env default for a ``topic:<slug>`` service — whether its slug is
    listed in ``PRECIS_TOPICS_ENABLED`` (``topics_env``).

    Mirrors :func:`_axis_id_default_on`: returns ``None`` for a non-topic
    service (the caller falls back to the registry/profile default). A
    ``topic:<slug>`` service has no ``ServiceSpec`` of its own (the spec is
    named ``classify_topics``) — this is where the per-topic env seed is
    read for the per-cycle gate.
    """
    prefix = "topic:"
    if service.startswith(prefix):
        return service[len(prefix) :] in topics_env
    return None


class _ResolverLike(Protocol):
    """The slice of :class:`ServiceConfigResolver` this pure helper needs —
    lets a test stub in without a DB-backed ``Store``."""

    def enabled(self, service: str, *, default_on: bool) -> bool: ...


def _classify_topics_enabled_slugs(
    resolver: _ResolverLike,
    *,
    only: str | None,
    global_on: bool,
    topics_env: frozenset[str],
    slugs: list[str],
) -> list[str] | None:
    """Enabled topic slugs for the ``classify_topics`` pass, or ``None`` for
    the full taxonomy.

    ``--only classify_topics`` and ``PRECIS_CLASSIFY_TOPICS_ENABLED=1`` are
    the admin full-backfill hatches (preserved the pre-0068
    meaning): the former always sweeps every topic (a single-pass,
    node-targeted invocation shouldn't be silently narrowed by per-topic
    gates); the latter is the legacy "all topics default-on" env seed, still
    refinable per-topic by a ``service_config`` row or
    ``PRECIS_TOPICS_ENABLED``.
    """
    if only == "classify_topics":
        return None
    return [
        s
        for s in slugs
        if resolver.enabled(f"topic:{s}", default_on=global_on or s in topics_env)
    ]


# ── ref-pass scheduling priority ──────────────────────────────────
#
# ``run_loop`` (workers/runner.run_loop) walks ``ref_passes`` in list
# order, sequentially, once per cycle: a slow pass registered ahead of
# another delays every pass behind it by its full batch duration. That
# is how a post-outage ``fetch_oa`` backlog once froze the planner —
# the fetch pass monopolised the single worker thread for tens of
# minutes each cycle while ``dispatch`` (which mints the planner's
# ``plan_tick`` jobs) sat near the end of the registration list and
# never got a turn, so freshly-created ``meta.llm_tier``-set todos went
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
#
# The bands are an explicit :class:`PassBand` enum rather than bare
# ints so the intent reads at a glance, and every key here is guarded
# by ``test_ref_pass_priority_keys_match_registered_passes``: it parses
# this module's AST and fails if a key no longer matches a live
# ``ref_passes.append(_<name>_pass)`` site — so renaming a band-assigned
# closure can no longer *silently* drop it into the DEFAULT band.


class PassBand(IntEnum):
    """Scheduling band for a ref-pass; lower runs earlier each cycle."""

    JOB = 0  # job execution: plan_tick coroutine + other job runners
    PLANNER = 10  # planner lifecycle: unblock, schedule, mint, unstick
    HEALTH = 15  # cheap SQL health
    DEFAULT = 20  # enrichment / indexing / reconcile / plugins
    BACKGROUND = 30  # heavy tail: external fetch + LLM + reviewers


_REF_PASS_PRIORITY: dict[str, PassBand] = {
    "_job_claude_inproc_pass": PassBand.JOB,
    "_job_coordinator_pass": PassBand.JOB,
    "_job_ssh_node_pass": PassBand.JOB,
    "_job_inproc_pass": PassBand.JOB,
    "_job_claude_docker_pass": PassBand.JOB,
    "_wake_runner_pass": PassBand.JOB,
    "_auto_check_pass": PassBand.PLANNER,
    "_schedule_pass": PassBand.PLANNER,
    "_scheduler_pass": PassBand.PLANNER,
    "_dispatch_pass": PassBand.PLANNER,
    "_sweeper_pass": PassBand.PLANNER,
    "_nursery_pass": PassBand.HEALTH,
    "_heartbeat_pass": PassBand.HEALTH,
    "_chase_pass": PassBand.BACKGROUND,
    "_bib_parse_pass": PassBand.BACKGROUND,
    "_bib_retag_pass": PassBand.DEFAULT,
    "_inbound_chase_pass": PassBand.BACKGROUND,
    "_hub_refine_pass": PassBand.BACKGROUND,
    "_hub_tagline_pass": PassBand.BACKGROUND,
    "_chase_trigger_pass": PassBand.BACKGROUND,
    "_fetch_pass": PassBand.BACKGROUND,
    "_gp_fetch_pass": PassBand.BACKGROUND,
    "_stub_rank_pass": PassBand.BACKGROUND,
    "_llm_summarize_pass": PassBand.BACKGROUND,
    "_classify_pass": PassBand.BACKGROUND,
    "_llm_reconcile_pass": PassBand.BACKGROUND,
    "_paper_glossary_pass": PassBand.BACKGROUND,
    "_paper_rank_pass": PassBand.BACKGROUND,
    "_classify_topics_pass": PassBand.BACKGROUND,
    "_axis_pass": PassBand.BACKGROUND,
    "_mail_poll_pass": PassBand.BACKGROUND,
    "_inject_scan_pass": PassBand.BACKGROUND,
    "_structural_pass": PassBand.BACKGROUND,
    "_deep_review_pass": PassBand.BACKGROUND,
    "_dream_agent_pass": PassBand.BACKGROUND,
}


def _ref_pass_priority(fn: RefPass) -> PassBand:
    """Scheduling band for a ref-pass closure (lower runs earlier)."""
    return _REF_PASS_PRIORITY.get(getattr(fn, "__name__", ""), PassBand.DEFAULT)


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
            "queue table — the worker discovers work by "
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
        choices=("system", "agent", "all"),
        default="system",
        help="Which pass rotation to run. 'system' (default) = the "
        "everything-except-heavy-LLM rotation: embed, summarize, "
        "chunk_keywords, chase, fetch, tag_embeddings, auto_check, "
        "schedule, nursery, dispatch, sweeper. 'agent' = the OAuth "
        "rotation: scheduler, job_claude_inproc, quota_check. "
        "dream_agent/structural/deep_review are NOT in either rotation — "
        "each is cadence-fired via the scheduler lease (workers/"
        "scheduler.py CADENCES: dream_agent + anki_sync host-pinned "
        "melchior, structural + deep_review dark-switched on any eligible "
        "host, gr192752) instead of the profile's per-cycle slot, so a "
        "wedged host can't starve them. job_claude_inproc/quota_check "
        "gate themselves via the PRECIS_LOAD_CEILING load-avg gate, so an "
        "agent profile worker that hits a tick with nothing to do exits "
        "in milliseconds. 'all' = the union of both rotations (§L-a "
        "collapsed-worker enablement, one-worker-per-host) — dark until a "
        "§L-b playbook run actually renders a unit with it; unused by any "
        "deployed unit today. Slice-5 consolidation: deploy one "
        "LaunchDaemon per profile.",
    )
    p.add_argument(
        "--only",
        choices=(
            "embed",
            "summarize",
            "chunk_keywords",
            "bib_parse",
            "bib_retag",
            "chase",
            "fetch",
            "gp_fetch",
            "stub_rank",
            "tag_embeddings",
            "job_claude_inproc",
            "job_inproc",
            "job_ssh_node",
            "dream_agent",
            "auto_check",
            "schedule",
            "nursery",
            "heartbeat",
            "structural",
            "deep_review",
            "dispatch",
            "sweeper",
            "quota_check",
            "disk_check",
            "watch_poll",
            "news_poll",
            "parts_refresh",
            "mail_poll",
            "inject_scan",
            "briefing",
            "llm_summarize",
            "classify",
            "llm_reconcile",
            "paper_glossary",
            "classify_topics",
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
        default=float(os.environ.get("PRECIS_EMBEDDER_TIMEOUT", "300.0")),
        help="Per-call HTTP deadline in seconds for --embedder remote "
        "(default: PRECIS_EMBEDDER_TIMEOUT env, else 300.0 — a CPU-host "
        "batch can legitimately take longer than 30s; a client timeout "
        "shorter than the server's compute time just amplifies retries, "
        "see embedder-wedge-hardening.md §5).",
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
    # (plan_tick, sandbox, claude -p) don't inherit it (secrets-vault).
    from precis import secrets as _secrets

    _secrets.adopt_process_store(store)
    # Bind the same store for the full LLM interaction log (route_log, migration
    # 0061) so every dispatch() call this worker makes is captured. Best-effort;
    # dark until bound.
    from precis import route_log as _route_log

    _route_log.bind_store(store)
    # Bind the same store for the budget circuit breaker's rolling meter, so
    # this worker's expensive dispatches are gated by the global cap.
    from precis.budget import bind_store as _bind_budget_store

    _bind_budget_store(store)
    # Bind the same store for DB-resident settings (precis.settings) so
    # registered keys resolve through the DB tier, mirroring secrets.
    from precis import settings as _settings

    _settings.bind_store(store)
    # Attach the centralised DB log handler now that we have a
    # working DSN. The file handler the worker's parent process
    # already set up stays in place as the bootstrap + fallback
    # channel (the DB handler degrades to it on flush failure).
    # Migration 0015 introduced worker_logs; older DBs that haven't
    # been migrated will fail INSERTs gracefully via the demote
    # path, so unattended deploys to a fresh DB don't die at boot.
    _attach_db_log_handler(dsn)
    _record_boot_event(store, profile=args.profile)
    # Worker boot epoch (lease-epoch reclaim): mint + advertise THIS
    # process's generation NOW, before ref_passes ever registers — both
    # profiles pass through here, and a claim can happen seconds after boot,
    # well inside the heartbeat pass's 60s throttle window. Without this the
    # epoch-reclaim arm in executors/_common.py would see a stale/absent
    # boot_id for up to a minute post-restart.
    from precis.workers.heartbeat import advertise_boot_id_now

    try:
        advertise_boot_id_now(store)
    except Exception:
        log.warning("worker: boot_id advertise failed", exc_info=True)
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
        # Profile membership + the extra ``PRECIS_*_ENABLED`` gates are
        # now declared once in ``workers/registry.py`` (the ServiceSpec
        # table) — the source of truth the factory console + capability
        # scheduler read too (docs/backlog/factory-console-and-scheduling.md).
        # ``system`` = every node's rotation (embed/summarize/… + the
        # coordinator/ssh_node/clusterize/reconcile passes that ship on
        # every node); ``agent`` = melchior's OAuth worker (the opus
        # reviewers + job_claude_inproc + quota_check). dream_agent stays
        # out of both profiles' rotation on THIS gate — its trigger is the
        # ``dream_agent`` scheduler cadence (§A, host-pinned melchior, see
        # workers/scheduler.py), not a profile default; ``--only dream_agent``
        # still self-gates via PRECIS_DREAM_AGENT=1 for a manual run. A
        # totality test (``tests/test_worker_registry.py``) fails CI if a
        # pass wired below has no spec, so the table can't drift from the code.
        profile_passes = service_names_for_profile(args.profile)

        # Live run control (factory slice 2): the service_config table can
        # override a pass's env/profile default per host — prio 0 forces it
        # off, prio >= 1 forces it on — picked up on the next loop cycle
        # without a redeploy. An empty table ⇒ byte-identical to the
        # env/profile defaults. A short TTL keeps the per-cycle gate a dict
        # lookup, not a query per pass per cycle.
        from precis.corpus_layout import host_name

        _svc_resolver = ServiceConfigResolver(store, host_name())

        def _profile_default_on(name: str) -> bool:
            """The structural (profile-membership) default for ``name``,
            before any ``service_config`` override.

            §L control cutover: ``PRECIS_*_ENABLED`` is retired as a default
            source — a formerly dark-switched pass (registry ``enable_env`` set)
            now defaults OFF absent an explicit row (seeded at deploy time
            from today's live flag state, see the seed task in
            ``deploy/roles/precis_worker*``); only profile-rotation
            membership still contributes a default-ON verdict — ANDed
            (gr193672) with the spec's ``capability_env`` being satisfied
            on this host, so ``--profile all``'s union can't default an
            agent-capability pass ON where it cannot run.
            """
            return name in profile_passes and _capability_ok(name, os.environ)

        def _register(name: str) -> bool:
            """Boot gate: whether ``name`` registers into ``ref_passes`` on
            this invocation. See :func:`_should_register` — this just binds
            it to the current invocation's ``--only`` / ``--profile``."""
            return _should_register(args.only, name, profile_passes=profile_passes)

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
        # gr191264: whether the in-rotation heartbeat pass registered below —
        # used after the loop to decide whether to also start the dedicated
        # heartbeat thread (see that site for why).
        heartbeat_registered = False
        if _register("chunk_keywords"):
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

        # bib_parse — per-paper bibliography parse + DOI match
        # (``citation-bib-parse`` (git-only)). Default-ON like
        # chunk_keywords above: the `meta.bib_parse_version` predicate
        # converges, so normal cadence drains the backlog; `--only
        # bib_parse` is the fast-path burst backfill. SMALL-tier LLM via
        # the router (dispatch seam) for the messy-line parse
        # fallback + close-candidate Crossref adjudication; Crossref
        # itself goes through `safe_get` (SSRF guard), not this client.
        if _register("bib_parse"):
            from precis.utils.llm.router import DispatchClient as _BpDispatchClient
            from precis.utils.llm.router import Tier as _BpTier
            from precis.workers.runner import BatchResult as _BpBatchResult

            # A ~20-entry parse-fallback batch (structured JSON per line) or a
            # candidate-adjudication reply is far larger than the 220-token
            # 2-part-summary the SMALL default caps at — that budget truncates
            # the JSON mid-object so it never parses (every batch "fails").
            # Pin the room via `max_tokens`, same as `paper_glossary` below;
            # env-overridable.
            _bp_client = _BpDispatchClient(
                tier=_BpTier.SMALL,
                model=os.environ.get("PRECIS_BIB_PARSE_MODEL") or "summarizer",
                max_tokens=int(os.environ.get("PRECIS_BIB_PARSE_MAX_TOKENS") or 2000),
                source="bib_parse",
                log_call=True,
                log_blobs=False,
            )
            _bp_mailto = settings.get_str("contact.polite_email") or ""

            def _bib_parse_pass(batch_size: int) -> _BpBatchResult:
                from precis.workers.bib_parse import run_bib_parse_pass

                r = run_bib_parse_pass(
                    store,
                    client=_bp_client,
                    batch_size=min(batch_size, 4),
                    mailto=_bp_mailto,
                )
                return _BpBatchResult(
                    handler="bib_parse",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_bib_parse_pass)

        # bib_mark — inline citation-marker extraction into chunk_citations
        # (the shipped citation-taproot-resolve proposal, git
        # history). Default-ON like
        # bib_parse above and drains the same way (a BIBMARK:<version> chunk
        # tag done-marker converges, so normal cadence drains the backlog;
        # `--only bib_mark` is the fast-path burst). Pure regex over body
        # chunks of papers that already have paper_bib_entries rows — no
        # LLM, no external call, no embedder dependency.
        if _register("bib_mark"):
            from precis.workers.bib_mark import run_bib_mark_pass

            def _bib_mark_pass(batch_size: int) -> BatchResult:
                r = run_bib_mark_pass(store, batch_size=batch_size)
                return BatchResult(
                    handler="bib_mark",
                    claimed=r["chunks_swept"],
                    ok=r["chunks_swept"],
                    failed=r["failed"],
                )

            ref_passes.append(_bib_mark_pass)

        # bib_retag — Layer-2 corpus remediation (gripe 196447). Retypes
        # mis-typed `chunk_kind='paragraph'` bibliography chunks to
        # `references` (content-detected via bib_parse's shared detector) and
        # deletes their stale chunk_embeddings/chunk_summaries so they drop out
        # of semantic search. MUTATES existing corpus, so DEFAULT-OFF (registry
        # `enable_env`, no `default_profiles`): registers but is gated off every
        # cycle absent an explicit service_config row — run it deliberately via
        # `--only bib_retag`. `PRECIS_BIB_RETAG_DRY_RUN=1` makes it a
        # non-mutating count. Pure regex, no LLM / external call / embedder.
        if _register("bib_retag"):
            # Local import (aliased) — `--only bib_retag` skips the
            # chunk_keywords block that binds the bare `BatchResult`, so this
            # block must not depend on it (mirrors `_bib_parse_pass`).
            from precis.workers.bib_retag import run_bib_retag_pass
            from precis.workers.runner import BatchResult as _BrBatchResult

            def _bib_retag_pass(batch_size: int) -> _BrBatchResult:
                r = run_bib_retag_pass(store, batch_size=batch_size)
                return _BrBatchResult(
                    handler="bib_retag",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_bib_retag_pass)

        # Finding-chase pass — same sibling-worker pattern, but for
        # STATUS:tracing/acquiring findings. Default-off LLM hooks via
        # --with-llm or PRECIS_CHASE_LLM=1. The sibling-vs-base-class
        # rationale lives in the ``precis.workers`` package docstring.
        if _register("chase"):
            from precis.workers.chase import (
                _TAPROOT_CHASE_ENV,
                run_finding_chase_pass,
            )
            from precis.workers.embed import EmbedHandler
            from precis.workers.runner import BatchResult as _BatchResult

            # The embedder threaded as ``taproot_embedder`` is dual-purpose
            # (workers/chase.py's ``advance_finding`` docstring): the
            # Taproot Phase-3 W1 forward bridge's ``canon.block`` ANN
            # lookup, AND (acquisition-mode findings) the acquiring
            # arm's claim-text grounding search over a newly-fetched
            # stub's chunks. Both degrade gracefully to a deterministic
            # fallback with no embedder (the bridge no-ops; the
            # acquiring arm falls back to lexical overlap, same idiom as
            # the tracing arm's own ``_select_target_chunk``) -- so this
            # deliberately does NOT eagerly resolve a fresh embedder by
            # default. ``--embedder`` defaults to the REAL ``bge-m3``
            # model (a multi-GB download/load) -- constructing one on
            # every ordinary ``chase`` pass boot (this pass is NOT
            # default-off, unlike ``hub_refine``/``chase_trigger`` below)
            # would turn a deterministic-by-default worker into one that
            # silently depends on a model load, tests included. Reuse the
            # already-booted EmbedHandler's embedder when one exists (no
            # extra load — safe to hold onto even if neither consumer
            # fires); only eagerly construct fresh when the taproot flag
            # AND the LLM hooks are both on (the same "worth paying for
            # it" gate the bridge alone used before this feature). Any
            # construction failure degrades to ``None`` rather than
            # taking the whole pass down.
            _chase_embed_handler = next(
                (h for h in handlers if isinstance(h, EmbedHandler)), None
            )
            _chase_taproot_flag_on = bool(
                int(os.environ.get(_TAPROOT_CHASE_ENV, "0") or "0")
            )
            _chase_with_llm = args.with_llm or env_flag("PRECIS_CHASE_LLM")
            if _chase_embed_handler is not None:
                chase_embedder = _chase_embed_handler.embedder
            elif _chase_taproot_flag_on and _chase_with_llm:
                try:
                    chase_embedder = _resolve_embedder(args, store)
                except Exception:
                    log.warning(
                        "chase: embedder unavailable -- taproot bridge "
                        "will degrade to no-op",
                        exc_info=True,
                    )
                    chase_embedder = None
            else:
                if _chase_taproot_flag_on and not _chase_with_llm:
                    log.warning(
                        "chase: %s is on but no LLM hook is enabled "
                        "(--with-llm / PRECIS_CHASE_LLM) — the taproot "
                        "forward bridge needs a verifier verdict to do "
                        "anything, so no embedder is being loaded for it "
                        "(acquisition-mode grounding still degrades to "
                        "lexical matching without one)",
                        _TAPROOT_CHASE_ENV,
                    )
                chase_embedder = None

            def _chase_pass(batch_size: int) -> _BatchResult:
                r = run_finding_chase_pass(
                    store,
                    limit=batch_size,
                    with_llm=args.with_llm,
                    taproot_embedder=chase_embedder,
                )
                return _BatchResult(
                    handler="finding_chase",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_chase_pass)

        # inbound_chase — inbound counterpart to the finding-chase pass
        # above: exhaustive one-hop citer sweep + chunk-level verdicts
        # for papers ``PaperHandler.get`` has flagged ``INBOUND:pending``.
        # Default-OFF (`service prio` / --only inbound_chase — §L retired
        # PRECIS_INBOUND_CHASE_ENABLED as the live gate) — the exhaustive-
        # no-cap policy leans on the
        # (unshipped) global spend circuit breaker as its cost backstop;
        # see workers/inbound_chase.py's module docstring. Always runs
        # with LLM verification once claiming work — gated by this flag
        # alone, independent of --with-llm/PRECIS_CHASE_LLM.
        if _register("inbound_chase"):
            from precis.workers.inbound_chase import run_inbound_chase_pass
            from precis.workers.runner import BatchResult as _InboundBatchResult

            def _inbound_chase_pass(batch_size: int) -> _InboundBatchResult:
                r = run_inbound_chase_pass(store, limit=batch_size)
                return _InboundBatchResult(
                    handler="inbound_chase",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_inbound_chase_pass)

        # hub_refine — periodic, converging enrichment of EXISTING taproot
        # claim hubs (lifecycle stage 5, `precis.taproot` docstring): per due hub,
        # semantic-search the corpus for corroborating paper chunks,
        # LLM-verify, attach the survivors. Default-OFF — dark like every
        # other taproot service (§L: enable via `service prio '*' hub_refine
        # <n>`, no PRECIS_TAPROOT_REFINE_ENABLED any more) / --only hub_refine.
        # Needs an embedder for discovery (no separate --with-llm gate:
        # reaching this pass at all already implies paying for the verify
        # calls); reuse the already-booted EmbedHandler's embedder when one
        # exists (cheap — no model load), else construct fresh, LAZILY on
        # first actual invocation (not at registration): §L registers this
        # pass unconditionally on every profile, so eagerly resolving a
        # fresh embedder here would load bge-m3 on every agent-profile boot
        # even when the pass never turns on. Any construction failure
        # degrades to a logged no-op rather than taking the whole worker
        # down (mirrors the chase forward bridge's own embedder-unavailable
        # degrade, see workers/hub_refine.py's module docstring).
        if _register("hub_refine"):
            from precis.workers.embed import EmbedHandler as _HubRefineEmbedHandler
            from precis.workers.hub_refine import run_hub_refine_pass
            from precis.workers.runner import BatchResult as _HubRefineBatchResult

            _hub_refine_embed_handler = next(
                (h for h in handlers if isinstance(h, _HubRefineEmbedHandler)), None
            )
            _hub_refine_embedder_cache: list[Any] = []

            def _hub_refine_get_embedder() -> Any:
                if _hub_refine_embedder_cache:
                    return _hub_refine_embedder_cache[0]
                if _hub_refine_embed_handler is not None:
                    embedder = _hub_refine_embed_handler.embedder
                else:
                    try:
                        embedder = _resolve_embedder(args, store)
                    except Exception:
                        log.warning(
                            "hub_refine: embedder unavailable -- pass will "
                            "degrade to no-op",
                            exc_info=True,
                        )
                        embedder = None
                _hub_refine_embedder_cache.append(embedder)
                return embedder

            def _hub_refine_pass(batch_size: int) -> _HubRefineBatchResult:
                r = run_hub_refine_pass(store, embedder=_hub_refine_get_embedder())
                return _HubRefineBatchResult(
                    handler="hub_refine",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_hub_refine_pass)

        # hub_tagline — LLM backfill of `refs.meta.tagline` (a 3-6 word
        # human handle) on live taproot claim hubs missing one
        # (precis.workers.hub_tagline). Claim-and-lease per hub, one
        # SMALL-tier call, code-validated before write -- no embedder
        # needed. Default-OFF, dark like every other taproot service (§L:
        # `service prio` controls it, no PRECIS_HUB_TAGLINE_ENABLED live
        # read) / --only hub_tagline.
        if _register("hub_tagline"):
            from precis.workers.hub_tagline import run_hub_tagline_pass
            from precis.workers.runner import BatchResult as _HubTaglineBatchResult

            def _hub_tagline_pass(batch_size: int) -> _HubTaglineBatchResult:
                r = run_hub_tagline_pass(store, limit=batch_size)
                return _HubTaglineBatchResult(
                    handler="hub_tagline",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_hub_tagline_pass)

        # chase_trigger — incremental counterpart to hub_refine above
        # (transient-napping-parrot Phase 1): sweeps freshly-embedded
        # paper/patent chunks against the (tiny) claim-embedding index and
        # marks a near claim hub TAPROOT_DUE, so hub_refine's due-set claim
        # query picks it up promptly instead of waiting out its 90d
        # backstop. Default-OFF — dark like every other taproot service (§L:
        # `service prio` controls it now, no PRECIS_TAPROOT_CHASE_TRIGGER_
        # ENABLED). Needs an embedder (both to embed claim sentences and to
        # compare chunk vectors against them); same reuse-the-booted-
        # EmbedHandler-or-construct-fresh-LAZILY pattern as hub_refine above
        # (register-all means this closure is built on every profile, so the
        # fresh-embedder fallback must not fire until the pass first actually
        # runs), same embedder-unavailable no-op degrade.
        if _register("chase_trigger"):
            from precis.workers.chase_trigger import run_chase_trigger_pass
            from precis.workers.embed import EmbedHandler as _ChaseTriggerEmbedHandler
            from precis.workers.runner import BatchResult as _ChaseTriggerBatchResult

            _chase_trigger_embed_handler = next(
                (h for h in handlers if isinstance(h, _ChaseTriggerEmbedHandler)), None
            )
            _chase_trigger_embedder_cache: list[Any] = []

            def _chase_trigger_get_embedder() -> Any:
                if _chase_trigger_embedder_cache:
                    return _chase_trigger_embedder_cache[0]
                if _chase_trigger_embed_handler is not None:
                    embedder = _chase_trigger_embed_handler.embedder
                else:
                    try:
                        embedder = _resolve_embedder(args, store)
                    except Exception:
                        log.warning(
                            "chase_trigger: embedder unavailable -- pass will "
                            "degrade to no-op",
                            exc_info=True,
                        )
                        embedder = None
                _chase_trigger_embedder_cache.append(embedder)
                return embedder

            def _chase_trigger_pass(batch_size: int) -> _ChaseTriggerBatchResult:
                r = run_chase_trigger_pass(
                    store,
                    embedder=_chase_trigger_get_embedder(),
                    batch_size=batch_size,
                )
                # chase_trigger's own return shape ({claim_embeds,
                # chunks_swept, due_marked, failed}) doesn't carry a
                # {claimed, ok, failed} triple. Count BOTH chunks swept and
                # claim-embeddings refreshed as work units, so a cycle that
                # only refreshed stale claim vectors (chunks_swept=0) still
                # reports claimed>0 and isn't misread as idle by the runner
                # (runner.py: claimed>0 => any_work => no idle backoff).
                _worked = r["chunks_swept"] + r["claim_embeds"]
                return _ChaseTriggerBatchResult(
                    handler="chase_trigger",
                    claimed=_worked + r["failed"],
                    ok=_worked,
                    failed=r["failed"],
                )

            ref_passes.append(_chase_trigger_pass)

        # Hierarchical SOM cluster maps (precis-web /clusters grid).
        # Time-gated full rebuild per scope; see workers/clusterize.py.
        if _register("clusterize"):
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
        if _register("tag_embeddings"):
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
        if _register("job_claude_inproc"):
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

        # quest_loop_reconcile — the autonomous quest-loop reconciler (rung 4d,
        # replacing the old inline-tick allocator pass). Ships DARK: registers
        # (§L register-all) on the agent profile only — structural, not a live
        # flag — and stays gated off each cycle until a `service prio '*'
        # quest_loop_reconcile <n>` row lands (its spec.enable_env,
        # PRECIS_QUEST_LOOP_ENABLED, now only seeds the deploy-time row; see
        # test_quest_loop_reconcile_gate_env_matches_registration). It does
        # NOT tick a quest itself — each active quest owns a perpetual
        # `quest_tick` coordinator campaign (workers/job_types/quest_tick.py)
        # that harvests/reviews/proposes/dispatches on its own event-driven
        # cadence; this pass just guarantees that loop exists, self-healing one
        # that rested (a coordinator job idem-keyed `quest_tick:<id>` re-mints
        # once its predecessor reaches a terminal status). See
        # precis.quest.loop.reconcile_quest_loops. §L-a: 'all' (the collapsed
        # one-worker-per-host profile) carries every agent-only pass too, so
        # it registers there as well — 'system' alone still never does.
        if args.profile in ("agent", "all") and _register("quest_loop_reconcile"):
            from precis.quest.loop import reconcile_quest_loops
            from precis.workers.runner import BatchResult as _QBatchResult

            def _quest_loop_reconcile_pass(batch_size: int) -> _QBatchResult:
                r = reconcile_quest_loops(store, enabled=True)
                return _QBatchResult(
                    handler="quest_loop_reconcile",
                    claimed=int(r.get("ensured", 0)),
                    ok=int(r.get("minted", 0)),
                    failed=0,
                )

            ref_passes.append(_quest_loop_reconcile_pass)

        # job_coordinator — drains the `kind='job'` queue for jobs
        # whose meta.executor=='coordinator'. These are long-running
        # orchestrators (precis-dft's dft_campaign is the first
        # consumer) that run one short slice per pass and yield
        # between phases. See workers/executors/coordinator.py.
        if _register("job_coordinator"):
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
        if _register("job_ssh_node"):
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

        # job_inproc — §F cycle a: the generic bounded in-proc lane
        # (embed_batch is the first job_type). One job per pass tick
        # (limit=1) — a claimed job runs synchronously and must self-limit
        # its own work (minutes, not hours). See
        # workers/executors/job_inproc.py.
        if _register("job_inproc"):
            from precis.workers.executors.job_inproc import run_job_inproc_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _job_inproc_pass(batch_size: int) -> _BatchResult:
                r = run_job_inproc_pass(store, limit=1)
                return _BatchResult(
                    handler="job_inproc",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_job_inproc_pass)

        # job_claude_docker — drains sandbox_run jobs (meta.executor==
        # 'claude_docker') by launching a detached, cgroup-capped
        # container, polling it by name, and reaping it.
        # Registered **default-OFF** — only
        # via a `service prio` row (seeded once at deploy from the sandbox
        # hosts' group membership; §L retired PRECIS_SANDBOX_ENABLED as the
        # live gate) or an explicit `--only job_claude_docker`, mirroring
        # classify. So a deploy of this slice changes nothing until a human
        # enables it on a box with podman + a dedicated CLAUDE_CODE_OAUTH_TOKEN.
        if _register("job_claude_docker"):
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
        if _register("wake_runner"):
            from precis.workers.wake_runner import wake_pass_for_runner

            def _wake_runner_pass(batch_size: int) -> _BatchResult:
                return wake_pass_for_runner(store, batch_size)

            ref_passes.append(_wake_runner_pass)

        # llm_summarize — model-authored "very brief; some additional
        # detail" chunk summaries into chunk_summaries
        # (summarizer='llm-v1'), via the litellm `summarizer` alias.
        # Default-OFF: runs only via `--only llm_summarize` or a
        # `service prio` row (§L retired PRECIS_SUMMARIZE_LLM as the live
        # gate) — a 1M-chunk backfill is a deliberate,
        # node-targeted batch, not something every system worker should
        # pick up. See workers/llm_summarize.py.
        from precis.workers.llm_summarize import (
            SUMMARIZER_NAME,
            LlmConfig,
            run_llm_summarize_pass,
        )

        _summarize_cfg = LlmConfig.from_env()
        if _register("llm_summarize"):
            from precis.utils.llm.router import DispatchClient as _DispatchClient
            from precis.utils.llm.router import Tier as _Tier
            from precis.workers.runner import BatchResult as _BatchResult

            # Fold through the router — same ``.complete`` contract, but
            # the call gets the local-serving ``served_by`` reroute (Phase-2
            # litellm-retire). model=None ⇒ resolve_model(SMALL) =
            # ``PRECIS_SUMMARIZE_MODEL or summarizer`` = the config default, so
            # behaviour is byte-identical until a card declares served_by. The
            # per-chunk backfill logs a **lite** row (metadata + ref_id, no replay
            # blob) so local spend/wall-clock is mineable without a blob explosion.
            _summarize_client = _DispatchClient(
                tier=_Tier.SMALL,
                source="llm_summarize",
                log_call=True,
                log_blobs=False,
            )

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
        # (`service prio` or --only classify — §L retired
        # PRECIS_CLASSIFY_ENABLED as the live gate): a 1.3M-chunk
        # backfill is a deliberate, node-targeted batch, like llm_summarize.
        # Forces model=`summarizer` (PRECIS_SUMMARIZE_MODEL=qwen returns
        # empty — it's a thinking model). See workers/classify.py +
        # scripts/classify/EVAL_RESULTS.md.
        if _register("classify"):
            from precis.utils.llm.router import DispatchClient as _DispatchClient
            from precis.utils.llm.router import Tier as _Tier
            from precis.workers.runner import BatchResult as _ClsBatchResult

            # Resolve via the SMALL-tier chain (`llm.chain.small`)
            # rather than pinning the legacy `summarizer` litellm alias. The
            # chain's rungs (glm-4.7-flash → summarizer) are non-thinking small
            # models, so the empty-return hazard the old pin guarded against no
            # longer applies. PRECIS_CLASSIFY_MODEL still overrides (pin / tests).
            # Per-chunk high volume ⇒ a lite row (metadata, no replay blob).
            _cls_client = _DispatchClient(
                tier=_Tier.SMALL,
                model=os.environ.get("PRECIS_CLASSIFY_MODEL") or None,
                source="classify",
                log_call=True,
                log_blobs=False,
            )
            # Tier 2: a re-judge-only client bound to the
            # escalate model — must be a *distinct* client from `_cls_client`
            # (mirrors inject_scan's identical gate/escalate shape), else the
            # "escalate" call silently re-runs the same model twice.
            _cls_escalate_model = (
                os.environ.get("PRECIS_CLASSIFY_ESCALATE_MODEL") or None
            )
            _cls_escalate_client = (
                _DispatchClient(
                    tier=_Tier.SMALL,
                    model=_cls_escalate_model,
                    source="classify",
                    log_call=True,
                    log_blobs=False,
                )
                if _cls_escalate_model
                else None
            )

            def _classify_pass(batch_size: int) -> _ClsBatchResult:
                from precis.workers.classify import run_classify_pass

                # Concurrency (thread-pool width). Two sources, env wins:
                #  * PRECIS_CLASSIFY_CONCURRENCY — a *dedicated backfill* worker
                #    (`--only classify`) sets this to run wide, ISOLATED from the
                #    shared fleet: it bypasses the all-hosts `*` service_config
                #    row so cranking the blitz never spikes the calm system
                #    workers that read the live `/categorizers` knob.
                #  * else the live per-cycle `service_config` knob (slice 2,
                #    migration 0091): a `/categorizers` operator raises the pool
                #    width without a redeploy. NULL/no row -> 1 (serial).
                # run_classify_pass clamps either at PRECIS_CLASSIFY_MAX_CONCURRENCY.
                _env_conc = int(os.environ.get("PRECIS_CLASSIFY_CONCURRENCY") or 0)
                _concurrency = (
                    _env_conc
                    if _env_conc > 0
                    else _svc_resolver.concurrency("classify", default=1)
                )
                # Rows claimed per cycle. The shared loop caps at 16 (bounded
                # loop-hog); a backfill worker widens the claim via
                # PRECIS_CLASSIFY_BATCH_SIZE to keep a wide pool fed. (claim_limit
                # in run_classify_pass is max(batch_size, concurrency) regardless.)
                _env_batch = int(os.environ.get("PRECIS_CLASSIFY_BATCH_SIZE") or 0)
                _batch = _env_batch if _env_batch > 0 else min(batch_size, 16)
                r = run_classify_pass(
                    store,
                    client=_cls_client,
                    batch_size=_batch,
                    escalate_client=_cls_escalate_client,
                    concurrency=_concurrency,
                )
                return _ClsBatchResult(
                    handler="classify",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_classify_pass)

        # llm_reconcile — keep the llm-catalog model-card facts true against the
        # live OpenRouter feed + flag proxy drift (llm-catalog slice 1, step 1 of
        # the litellm teardown). Default-OFF (`service prio` / --only
        # llm_reconcile — §L retired PRECIS_LLM_RECONCILE_ENABLED as the live
        # gate); a single cheap app_state read until the catalog
        # has cards. Corpus-wide single-runner (app_state cadence + xact lock).
        if _register("llm_reconcile"):
            from precis.workers.runner import BatchResult as _LlmRecBatchResult

            def _llm_reconcile_pass(batch_size: int) -> _LlmRecBatchResult:
                from precis.workers.llm_reconcile import run_llm_reconcile_pass

                return run_llm_reconcile_pass(store)

            ref_passes.append(_llm_reconcile_pass)

        # paper_glossary — per-paper inferred reading glossary (reading-prep
        # loop, slice 1). Harvests Schwartz-Hearst abbreviations + undefined
        # acronyms + KeyBERT keywords and makes ONE LLM call to cluster+define
        # them into an embeddable `card_glossary` chunk (ord=-1000). Derived /
        # idempotent / reversible; NO account writes. Default-OFF
        # (`service prio` or --only paper_glossary — §L retired
        # PRECIS_PAPER_GLOSSARY_ENABLED as the live gate): a
        # corpus-wide backfill is a deliberate, node-targeted batch, like
        # classify. Model defaults to the cheap `summarizer` alias. See
        # workers/paper_glossary.py + docs/backlog/reading-prep-loop.md.
        if _register("paper_glossary"):
            from precis.utils.llm.router import DispatchClient as _DispatchClient
            from precis.utils.llm.router import Tier as _Tier
            from precis.workers.runner import BatchResult as _PgBatchResult

            # Fold through the router (dispatch seam) instead of holding
            # a raw litellm client — so this pass gets the breaker gate + the
            # local-serving ``served_by`` reroute (Phase-2 litellm-retire). A
            # glossary is a multi-term JSON object, far larger than the 220-token
            # 2-part summary the SMALL default caps at — that budget
            # truncates the JSON mid-object so it never parses (every paper
            # "fails"). Pin the room via ``max_tokens``; env-overridable.
            _pg_client = _DispatchClient(
                tier=_Tier.SMALL,
                model=os.environ.get("PRECIS_PAPER_GLOSSARY_MODEL") or "summarizer",
                max_tokens=int(
                    os.environ.get("PRECIS_PAPER_GLOSSARY_MAX_TOKENS") or 2000
                ),
                source="paper_glossary",
                log_call=True,
                log_blobs=False,
            )

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

        # paper_rank — deterministic five-signal reading-priority score per
        # `kind='paper'` ref, written to `meta.paper_rank` (feynman PaperRank
        # port; docs/backlog/feynman-paperrank-pass.md). Pure Python/SQL, no
        # LLM/network/embedder. Global-batch shape (NOT a per-ref claim):
        # each tick recomputes corpus-wide normalizers + a citation-graph
        # PageRank pass, then (re)writes only papers whose stored version is
        # stale, whose marker fingerprint (body-chunk-count:abstract-length)
        # changed, or whose recomputed composite has drifted > 0.5 from the
        # stored value. Default-OFF (`service prio` or --only paper_rank —
        # §L retired PRECIS_PAPER_RANK as the live gate): a corpus-wide
        # backfill is a deliberate, node-targeted batch, like bib_retag. See
        # workers/paper_rank.py.
        if _register("paper_rank"):
            from precis.workers.runner import BatchResult as _PrBatchResult

            def _paper_rank_pass(batch_size: int) -> _PrBatchResult:
                from precis.workers.paper_rank import run_paper_rank_pass

                r = run_paper_rank_pass(store, batch_size=batch_size)
                return _PrBatchResult(
                    handler="paper_rank",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_paper_rank_pass)

        # classify_topics — paper→topic-dossier cascade (per-topic
        # gating per-topic classify gating). Tier-0 free keyword screen, tier-1 cheap local
        # model confirms/expands the candidate set — MULTI-LABEL (a paper can
        # carry several `topic:` tags). Writes `topic:<slug>` open tags + a
        # `TOPICCASCADE:<marker>` marker keyed on the *enabled-topic set*
        # (bump CLASSIFY_TOPICS_VERSION, or flip a topic's own
        # `topic:<slug>` service, to re-tag the corpus lazily). Each topic is
        # independently flippable from the `/categorizers` console — this one
        # `classify_topics` pass still runs iff >=1 topic is enabled; an
        # explicit `classify_topics` row remains a global kill-switch
        # (`_gate_default_on` below). `--only classify_topics` and
        # PRECIS_CLASSIFY_TOPICS_ENABLED=1 are the admin full-taxonomy
        # backfill hatches (preserved the pre-0068 meaning): a
        # corpus-wide backfill is a deliberate, node-targeted batch, like
        # classify/paper_glossary. See workers/classify_topics.py +
        # docs/decisions/0060-topic-dossiers.md.
        _topics_env_set = env_csv_set("PRECIS_TOPICS_ENABLED")
        if _register("classify_topics"):
            from precis.utils.llm.router import DispatchClient as _DispatchClient
            from precis.utils.llm.router import Tier as _Tier
            from precis.workers.runner import BatchResult as _CtBatchResult

            _ct_client = _DispatchClient(
                tier=_Tier.SMALL,
                model=os.environ.get("PRECIS_CLASSIFY_TOPICS_MODEL") or "summarizer",
                source="classify_topics",
                log_call=True,
                log_blobs=False,
            )

            def _classify_topics_pass(batch_size: int) -> _CtBatchResult:
                from precis.workers.classify_topics import (
                    all_topic_slugs,
                    run_classify_topics_pass,
                )

                enabled = _classify_topics_enabled_slugs(
                    _svc_resolver,
                    only=args.only,
                    global_on=env_flag("PRECIS_CLASSIFY_TOPICS_ENABLED"),
                    topics_env=_topics_env_set,
                    slugs=all_topic_slugs(),
                )
                r = run_classify_topics_pass(
                    store,
                    client=_ct_client,
                    batch_size=min(batch_size, 16),
                    enabled_slugs=enabled,
                )
                return _CtBatchResult(
                    handler="classify_topics",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_classify_topics_pass)

        # axis — generic ``data/axes/<id>.yaml`` classifier runner, with prerequisite enforcement: an item is only eligible for
        # axis X once it already carries a tag in every namespace X's
        # `prereq:` lists (e.g. `material` waits for `domain`). Purely
        # additive — does not touch `classify` (junk/role3, which keep
        # their own cascade pass below) or `classify_topics`.
        #
        # Each axis :func:`~precis.workers.axis_pass.discover_axis_ids`
        # returns is its own `service_config`-gated service, `axis:<id>` —
        # independently flippable from the `/categorizers` console (the
        # `service_config` row's write target) without touching any other
        # axis. Default-OFF per id: PRECIS_AXES_ENABLED (comma-separated
        # ids) just seeds that id's env/profile default_on verdict — a
        # deploy-time convenience — but a live `service_config` row always
        # wins, mirroring `_gate_default_on`'s contract for every other named
        # pass. See workers/axis_pass.py + data/axes/README.md.
        from precis.workers.axis_pass import discover_axis_ids

        _axes_env_set = env_csv_set("PRECIS_AXES_ENABLED")
        _axis_ids = discover_axis_ids()
        if _axis_ids:
            from precis.utils.llm.router import DispatchClient as _DispatchClient
            from precis.utils.llm.router import Tier as _Tier
            from precis.workers.runner import BatchResult as _AxisBatchResult

            for _axis_id in _axis_ids:
                _axis_service = f"axis:{_axis_id}"
                # Register every axis unconditionally — the per-cycle pass_gate
                # decides whether it runs, so a /categorizers On-flip takes effect
                # without a worker restart. ``--only`` still restricts to the one
                # selected pass. (``_axes_env_set`` now feeds the gate's default,
                # not this boot check — see the ``pass_gate`` wiring below.)
                if not _register(_axis_service):
                    continue

                _axis_client = _DispatchClient(
                    tier=_Tier.SMALL,
                    model=os.environ.get("PRECIS_AXIS_MODEL") or "summarizer",
                    source=_axis_service,
                    log_call=True,
                    log_blobs=False,
                )

                def _axis_pass(
                    batch_size: int,
                    axis_id: str = _axis_id,
                    client: Any = _axis_client,
                ) -> _AxisBatchResult:
                    from precis.workers.axis_pass import run_axis_pass

                    r = run_axis_pass(
                        store,
                        dispatch=client,
                        axis_id=axis_id,
                        batch_size=min(batch_size, 16),
                    )
                    return _AxisBatchResult(
                        handler=f"axis:{axis_id}",
                        claimed=r["claimed"],
                        ok=r["ok"],
                        failed=r["failed"],
                    )

                # Per-cycle live gate (below, passed to run_loop as
                # ``pass_gate``): every ``_axis_pass`` closure shares the
                # same ``__name__``, so the name-derived service ("axis")
                # can't tell them apart — carry the real ``axis:<id>``
                # service explicitly (``runner.run_loop`` prefers this
                # attribute over the ``__name__`` derivation).
                _axis_pass.service_name = _axis_service  # type: ignore[attr-defined]
                ref_passes.append(_axis_pass)

        # briefing_audio — narrate the morning news briefing onto the podcast
        # feed (the first automatic audio producer). Default-OFF (`service prio` or --only briefing_audio — §L retired
        # PRECIS_BRIEFING_AUDIO_ENABLED as the live gate)
        # and TTS-host-only: it needs the `[tts]` extra + Kokoro model files +
        # ffmpeg + a PRECIS_PODCAST_DIR, all of which live on spark. Decoupled
        # from the briefing *job* (which runs on the agent worker, no TTS): this
        # pass reads the persisted `briefing-<date>` ref and self-schedules off
        # its existence, idempotent via a `meta.audio_episode_id` marker. See
        # workers/briefing_audio.py.
        if _register("briefing_audio"):
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
        # nidra) onto the podcast feed (docs/backlog/reading-prep-loop.md §Audio).
        # Same substrate as briefing_audio: TTS-host-only (spark), container-first,
        # self-scheduling off an un-narrated cast draft, idempotent via
        # meta.audio_episode_id. Default-OFF (`service prio` or --only
        # cast_audio — §L retired PRECIS_CAST_AUDIO_ENABLED as the live gate)
        # + needs PRECIS_TTS_IMAGE. See workers/cast_audio.py.
        if _register("cast_audio"):
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

        # Backlog groomer (default-OFF; `service prio` — §L retired
        # PRECIS_BACKLOG_GROOM_ENABLED as the live gate — or --only
        # backlog_groom): promote open gripes into dispatchable
        # ``fix_gripe`` todos so the autonomous fixer substrate acts on the
        # bug backlog. Off by default because enabling it starts handing
        # repo bugs to claude_inproc — a deliberate flip, like classify.
        if _register("backlog_groom"):
            from precis.workers.runner import BatchResult as _GroomBatchResult

            def _backlog_groom_pass(batch_size: int) -> _GroomBatchResult:
                from precis.workers.backlog_groom import run_backlog_groom_pass

                return run_backlog_groom_pass(store, batch_size=min(batch_size, 16))

            ref_passes.append(_backlog_groom_pass)

        # Diagnose scanner (default-OFF; PRECIS_DIAGNOSE_SCAN_ENABLED, the
        # backlog_groom dark-pass shape): mint read-only diagnose_gripe jobs
        # for open, undiagnosed gripes so the (separately dark) diagnosis
        # pass upgrades them with a pinned root cause before the expensive
        # fix_gripe / human sweep touches them. See
        # precis.workers.diagnose_scan and docs/backlog/dark-factory-arming.md.
        if _register("diagnose_scan"):
            from precis.workers.runner import BatchResult as _DiagBatchResult

            def _diagnose_scan_pass(batch_size: int) -> _DiagBatchResult:
                from precis.workers.diagnose_scan import run_diagnose_scan_pass

                return run_diagnose_scan_pass(store, batch_size=batch_size)

            ref_passes.append(_diagnose_scan_pass)

        # Plugin-registered ref passes: third-party packages can
        # ship their own background workers via the
        # ``precis.ref_passes`` entry-point group (precis-dft's
        # ``view_worker`` is the first consumer). Failure isolation
        # mirrors handler discovery — a broken plugin factory logs
        # a warning and the worker carries on with whatever did
        # register. The pass-name gate (``_register``) still
        # applies so ``--only`` and the profile pass set honour
        # plugin passes the same way they honour built-ins.
        from precis.workers._plugin_passes import (
            discover_plugin_ref_passes,
        )

        for pass_name, plugin_callable, plugin_profiles in discover_plugin_ref_passes(
            store, profile=args.profile, args=args
        ):
            if not _register(pass_name):
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
        if _register("fetch"):
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

        # Citation-forward watcher. Polls
        # Semantic Scholar for papers that cite our most-due salient
        # papers and mints metadata-only stubs; fetch_oa then OA-acquires
        # them. Deliberately NOT in system_passes/agent_passes — it makes
        # external S2 calls and must run on a cadence, not the hot loop.
        # Run it from a dedicated low-frequency cron via
        # ``precis worker --only watch_poll`` (mirrors the dream cron).
        if _register("watch_poll"):
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
        if _register("news_poll"):
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

        # parts_refresh — the JLCPCB catalog ingest (gr264357). Same shape
        # as health_digest/materialize below: this registration is for a
        # manual/ad-hoc `--only parts_refresh` run only (no
        # `default_profiles`, no `enable_env`). The STANDING trigger is the
        # `parts_refresh` scheduler-lease cadence (workers/scheduler.py
        # CADENCES, daily, host-agnostic) — the catalog moves slowly. Dark
        # (clean no-op) without JLCPCB Open API credentials.
        if _register("parts_refresh"):
            from precis.workers.parts_refresh import run_parts_refresh_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _parts_refresh_pass(batch_size: int) -> _BatchResult:
                # batch_size is unused, like health_digest/materialize
                # below — the pass caps itself via its own row budget
                # (workers/parts_refresh.py's DEFAULT_ROW_BUDGET).
                r = run_parts_refresh_pass(store)
                return _BatchResult(
                    handler="parts_refresh",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_parts_refresh_pass)

        # Mailbox poll (email kind, slice 3). Per-account IMAP poll for new
        # mail past the last_uid high-water + inline tier-0 regex injection
        # scan; verdicts land in email_scan (no body stored). Dark behind a
        # `service prio` row (§L retired PRECIS_MAIL_POLL_ENABLED as the live
        # gate; no default profile) so it runs on one host,
        # not every node against the same mailbox. Cadence + backoff are inside
        # the pass, so it's cheap to tick every cycle.
        if _register("mail_poll"):
            from precis.workers.mail_poll import run_mail_poll
            from precis.workers.runner import BatchResult as _BatchResult

            def _mail_poll_pass(batch_size: int) -> _BatchResult:
                r = run_mail_poll(store)
                return _BatchResult(
                    handler="mail_poll",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_mail_poll_pass)

        # Email injection scan (email kind, slice 4). The deep rung of the
        # cascade: leases tier-0 verdicts, re-fetches the body from IMAP, and
        # model-scores it (local `summarizer` alias by default) for a prompt-
        # injection attempt, escalating ambiguous ones + raising an alert on
        # `high`. Dark behind a `service prio` row (§L retired
        # PRECIS_INJECT_SCAN_ENABLED as the live gate; agent host, where the
        # local model proxy resolves); routed through the routing-seam DispatchClient.
        if _register("inject_scan"):
            from precis.utils.llm.router import DispatchClient as _InjDispatchClient
            from precis.utils.llm.router import Tier as _InjTier
            from precis.workers.inject_scan import run_inject_scan_pass
            from precis.workers.runner import BatchResult as _InjBatchResult

            _inj_client = _InjDispatchClient(
                tier=_InjTier.SMALL,
                model=os.environ.get("PRECIS_INJECT_SCAN_MODEL") or "summarizer",
                source="inject_scan",
            )
            _inj_escalate_model = os.environ.get("PRECIS_INJECT_SCAN_ESCALATE_MODEL")
            _inj_escalate = (
                _InjDispatchClient(
                    tier=_InjTier.SMALL,
                    model=_inj_escalate_model,
                    source="inject_scan",
                )
                if _inj_escalate_model
                else None
            )

            def _inject_scan_pass(batch_size: int) -> _InjBatchResult:
                r = run_inject_scan_pass(
                    store,
                    client=_inj_client,
                    escalate_client=_inj_escalate,
                    batch_size=min(batch_size, 8),
                )
                return _InjBatchResult(
                    handler="inject_scan",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_inject_scan_pass)

        # Morning briefing — summarizes recent `news` refs into a dated
        # digest ref. LLM-backed (summarizer alias), so cron-driven via
        # ``precis worker --only briefing`` (e.g. a daily launchd tick),
        # never the hot loop.
        if _register("briefing"):
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
        # even on hosts that shouldn't run it (external-fetch goodwill:
        # off by default, one polite retry).
        if _register("gp_fetch"):
            from precis.workers.fetch_google_patents import run_gp_fetch_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _gp_fetch_pass(batch_size: int) -> _BatchResult:
                # Cap at 1 per pass — patents.google.com is a third-
                # party host and we want only one in-flight request at
                # a time per host. Combined with the dark switch being
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
        if _register("auto_check"):
            from precis.workers.auto_check import run_auto_check_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _auto_check_pass(batch_size: int) -> _BatchResult:
                return run_auto_check_pass(store, limit=batch_size)

            ref_passes.append(_auto_check_pass)

        # Schedule pass — Slice 4 of todo-tree-plan.md. Walks recurring
        # (meta.schedule set) refs, mints subtasks for due ticks under
        # the Watches umbrella. SQL-only and idempotent
        # (meta.spawned_for_tick stamp), so it shares the default
        # cycle with auto_check.
        if _register("schedule"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.schedule import run_schedule_pass

            def _schedule_pass(batch_size: int) -> _BatchResult:
                return run_schedule_pass(store, limit=batch_size)

            ref_passes.append(_schedule_pass)

        # Scheduler pass — §15i, slice 10. LIVE (§A): the decentralized
        # recurring-work trigger, default-on for BOTH profiles (registry.py
        # ServiceSpec) — folds the standalone thin-timer daemons (cron tick,
        # watch poll, and now the host-pinned dream_agent / anki_sync
        # cadences too) into the worker via a DB-lease conditional advance
        # (exactly-once across the fleet, no designated node). It must run on
        # the agent profile too, or a host-pinned melchior cadence never has
        # an eligible claimant.
        if _register("scheduler"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.scheduler import run_scheduler_pass

            def _scheduler_pass(batch_size: int) -> _BatchResult:
                return run_scheduler_pass(
                    store, host=host_name(), batch_size=batch_size
                )

            ref_passes.append(_scheduler_pass)

        # Nursery pass — Slice 3 of todo-tree-plan.md. SQL-only
        # pattern matcher that surfaces local incoherence (orphans,
        # stale claims, long waits, stuck doable, stalled recurrings,
        # spin loops) as ``kind='alert'`` rows (one per condition,
        # deduped on fingerprint; cleared conditions auto-resolve).
        # Idempotent per pass — a still-present condition just bumps
        # its alert's seen_count, so the default rotation can include
        # this without spamming the table.
        if _register("nursery"):
            from precis.workers.nursery import run_nursery_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _nursery_pass(batch_size: int) -> _BatchResult:
                return run_nursery_pass(store, limit=batch_size)

            ref_passes.append(_nursery_pass)

        # Heartbeat pass — §A. Refactored core of `precis heartbeat`
        # (workers/heartbeat.py), now also running as a per-host system-
        # worker pass so a host's liveness signal doesn't depend solely on
        # its still-live launchd/systemd timer. Self-throttled in-process
        # (NOT scheduler_leases — heartbeat is the liveness signal that lease
        # machinery is judged by, so it must never depend on it); a
        # double-fire against the timer is a harmless idempotent upsert.
        if _register("heartbeat"):
            from precis.workers.heartbeat import run_heartbeat_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _heartbeat_pass(batch_size: int) -> _BatchResult:
                return run_heartbeat_pass(store)

            ref_passes.append(_heartbeat_pass)
            heartbeat_registered = True

        # Structural review pass — Slice 3 of todo-tree-plan.md. Opus-class
        # semantic review of the tree's shape (drift between outcomes and
        # child actions, sibling contradictions, depth/fanout warnings).
        # This registration is for a manual/ad-hoc `--only structural` run
        # only — gated by PRECIS_STRUCTURAL_REVIEW=1, same as always. The
        # STANDING trigger is now the `structural` scheduler cadence
        # (gr192752, workers/scheduler.py CADENCES), not the agent-profile
        # default rotation: a long `chase` pass could monopolize the
        # strictly-serial `--profile all` loop for hours and starve this
        # reviewer, so the fleet-wide lease (live on any host carrying
        # PRECIS_STRUCTURAL_REVIEW) replaces the in-rotation slot — the
        # old per-pass LaunchDaemon (cluster/roles/precis_structural) is
        # retired.
        if _register("structural"):
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.structural import run_structural_pass

            def _structural_pass(batch_size: int) -> _BatchResult:
                return run_structural_pass(store)

            ref_passes.append(_structural_pass)

        # Deep review pass — Slice 3 of todo-tree-plan.md. Weekly full
        # Allen-review; same shape as structural with a longer prompt,
        # longer timeout, larger turn cap, and a 144h dedup window. This
        # registration is for a manual/ad-hoc `--only deep_review` run
        # only — gated by PRECIS_DEEP_REVIEW=1. The STANDING trigger is
        # now the `deep_review` scheduler cadence (gr192752, same
        # rationale as `structural` just above).
        if _register("deep_review"):
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
        if _register("dispatch"):
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
        if _register("sweeper"):
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
        if _register("corpus_reconcile"):
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
        if _register("paper_reconcile"):
            from precis.workers.paper_reconcile import run_paper_reconcile_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _paper_reconcile_pass(batch_size: int) -> _BatchResult:
                return run_paper_reconcile_pass(store, limit=None)

            ref_passes.append(_paper_reconcile_pass)

        # OpenAlex abstract/metadata enrich — self-healing fill for
        # top-level abstracts (what the paper page reads). Promotes
        # already-fetched OpenAlex abstracts (no network) and drips a
        # small DOI-fetch batch each due pass. Same throttle + advisory-lock
        # guards as paper_reconcile, cadence via
        # PRECIS_OPENALEX_ENRICH_REFRESH_HOURS (default 6).
        if _register("openalex_enrich"):
            from precis.workers.openalex_enrich import run_openalex_enrich_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _openalex_enrich_pass(batch_size: int) -> _BatchResult:
                return run_openalex_enrich_pass(store, limit=None)

            ref_passes.append(_openalex_enrich_pass)

        # Paper metadata enrichment — re-resolve authors + entry_type/
        # journal/issn/idents/retraction status from one Crossref
        # (+conditional OpenAlex) fetch per paper. Wholesale author
        # replace (also flushes junk) unless human_verified_at is set;
        # DOI-less papers get a no-network heuristic split. Same
        # throttle + advisory-lock guards as openalex_enrich, cadence via
        # PRECIS_PAPER_META_ENRICH_REFRESH_HOURS (default 6).
        if _register("paper_meta_enrich"):
            from precis.workers.paper_meta_enrich import run_paper_meta_enrich_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _paper_meta_enrich_pass(batch_size: int) -> _BatchResult:
                return run_paper_meta_enrich_pass(store, limit=None)

            ref_passes.append(_paper_meta_enrich_pass)

        # Stub rank — S2-enrich + embed + anchor-similarity re-rank paper
        # stubs (title/abstract only, no PDF yet) so fetch_oa's claim
        # query and the stub-backlog surfaces float the relevant ones
        # instead of draining newest-first, plus a Tier-2 SMALL-tier LLM
        # band label for the uncertain middle of the score distribution.
        # See workers/stub_rank.py's module docstring for the four-step
        # (enrich/embed/rank/band) shape. Embedder resolution mirrors
        # `chase`'s degrade-on-failure pattern just above (never crash-loop
        # the whole worker over a down/unconfigured embedder) — the embed
        # step alone no-ops without one; enrich/rank/band still run.
        if _register("stub_rank"):
            from precis.utils.llm.router import DispatchClient as _DispatchClient
            from precis.utils.llm.router import Tier as _Tier
            from precis.workers.runner import BatchResult as _BatchResult
            from precis.workers.stub_rank import run_stub_rank_pass

            try:
                _stub_rank_embedder = _resolve_embedder(args, store)
            except Exception:
                log.warning(
                    "stub_rank: embedder unavailable -- embed step will "
                    "no-op until it recovers",
                    exc_info=True,
                )
                _stub_rank_embedder = None

            # SMALL-tier band client (like classify's `_cls_client` above) —
            # a lite route-log row (log_call=True, log_blobs=False) so the
            # per-stub cost/tokens land in llm_call_log without a per-call
            # blob explosion at corpus batch scale.
            _stub_rank_band_client = _DispatchClient(
                tier=_Tier.SMALL,
                model=os.environ.get("PRECIS_STUB_RANK_LLM_MODEL") or None,
                source="stub_rank",
                log_call=True,
                log_blobs=False,
            )

            def _stub_rank_pass(batch_size: int) -> _BatchResult:
                # limit=None (not batch_size): mirrors openalex_enrich /
                # paper_reconcile just above — this pass owns its own
                # default (PRECIS_STUB_RANK_ENRICH_BATCH, 500), independent
                # of the generic per-tick chunk batch size (32). Same for
                # band_limit=None -- PRECIS_STUB_RANK_LLM_BATCH is its own
                # cost guard.
                r = run_stub_rank_pass(
                    store,
                    limit=None,
                    embedder=_stub_rank_embedder,
                    band_client=_stub_rank_band_client,
                    band_limit=None,
                )
                return _BatchResult(
                    handler="stub_rank",
                    claimed=r["claimed"],
                    ok=r["ok"],
                    failed=r["failed"],
                )

            ref_passes.append(_stub_rank_pass)

        # Quota-check pass — refresh the Claude.ai OAuth utilisation
        # snapshot via one 1-token `claude -p "quota" --output-format
        # json` call. Agent profile only: hermes's OAuth state lives
        # there. Short-circuits when the persisted snapshot is younger
        # than REFRESH_INTERVAL_S (default 600s), so the cost is one
        # SQL probe per idle cycle + a 2-token completion every 10 min.
        if _register("quota_check"):
            from precis.workers.quota_check import run_quota_check_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _quota_check_pass(batch_size: int) -> _BatchResult:
                return run_quota_check_pass(store, limit=batch_size)

            ref_passes.append(_quota_check_pass)

        # Disk-check pass — gripe 191008: caspar's DB SSD hit 100%, psycopg
        # DiskFull stalled ALL prod writes ~1.5h before anyone noticed (the
        # Prometheus 90% rule reached no one). System profile, every node:
        # dfs the configured watch paths (default "/") and raises a
        # precis-native kind='alert' (warn/critical) so it lands in the
        # /alerts tab and, on a fresh critical, the ops Discord push.
        if _register("disk_check"):
            from precis.workers.disk_check import run_disk_check_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _disk_check_pass(batch_size: int) -> _BatchResult:
                return run_disk_check_pass(store, limit=batch_size)

            ref_passes.append(_disk_check_pass)

        # dream_agent — a Python-side dispatch through call_claude_agent
        # (loads the directive prompt + soul + MCP config from env-pointed
        # file paths; no Web tools, bypass permissions, 20 turns). This
        # registration is for a manual/ad-hoc `--only dream_agent` run only —
        # gated by PRECIS_DREAM_AGENT=1, same as always. The STANDING trigger
        # is now the `dream_agent` scheduler cadence (§A, host-pinned
        # melchior — workers/scheduler.py), which calls run_dream_pass
        # directly; the old standalone hermes-pinned 15-min LaunchDaemon
        # (dream-pass.sh, precis_dream role) is retired. The worker-agent
        # role now installs PRECIS_DREAM_AGENT on melchior's agent-profile
        # plist instead. The persona is packaged
        # (data/prompts/dream-persona.md) with no env override.
        if _register("dream_agent"):
            from precis.workers.dream_agent import run_dream_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _dream_agent_pass(batch_size: int) -> _BatchResult:
                return run_dream_pass(store)

            ref_passes.append(_dream_agent_pass)

        # health_digest — docs/backlog/self-healing-spine.md Layer 2. Same shape
        # as dream_agent just above: this registration is for a manual/ad-hoc
        # `--only health_digest` run only (no `default_profiles`, no
        # `enable_env`). The STANDING trigger is the `health_digest`
        # scheduler-lease cadence (workers/scheduler.py CADENCES, hourly,
        # host-agnostic — any live worker can win it), which calls
        # `run_health_digest_pass` directly.
        if _register("health_digest"):
            from precis.workers.health_digest import run_health_digest_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _health_digest_pass(batch_size: int) -> _BatchResult:
                return run_health_digest_pass(store)

            ref_passes.append(_health_digest_pass)

        # materialize — §F cycle a (docs/backlog/cluster-scheduling.md
        # §F). Same shape as health_digest just above: this registration is
        # for a manual/ad-hoc `--only materialize` run only (no
        # `default_profiles`, no `enable_env`). The STANDING trigger is the
        # `materialize` scheduler-lease cadence (workers/scheduler.py
        # CADENCES, 300s, host-agnostic), which calls `run_materialize_pass`
        # directly. DARK unless PRECIS_MATERIALIZE_EMBED=1 — see
        # workers/materialize.py.
        if _register("materialize"):
            from precis.workers.materialize import run_materialize_pass
            from precis.workers.runner import BatchResult as _BatchResult

            def _materialize_pass(batch_size: int) -> _BatchResult:
                return run_materialize_pass(store)

            ref_passes.append(_materialize_pass)

        # Real work before background I/O. The run loop is sequential
        # per cycle, so ordering is priority: job execution + planner
        # lifecycle must run ahead of slow fetch/enrichment/reviewer
        # passes or a fetch backlog starves ``dispatch`` and the
        # planner stalls. Stable sort ⇒ registration order preserved
        # within a band. See ``_REF_PASS_PRIORITY``.
        ref_passes.sort(key=_ref_pass_priority)

        # Per-cycle live gate: consulted each cycle for every registered pass so
        # a service_config flip takes effect within one cache TTL, no restart —
        # in BOTH directions. Any always-registered pass (§L: that's all of
        # them now, see ``_register``) turns ON when a prio>=1 row lands and
        # OFF on a prio 0 row. The baseline when NO row exists is NOT a blanket
        # ``True`` (that would run every always-registered pass unconditionally)
        # nor does it read ``PRECIS_*_ENABLED`` any more (§L retires that as a
        # run-control default — see ``_profile_default_on``): a formerly-env-
        # gated pass (classify/hub_refine/llm_summarize/…) with no row now
        # defaults OFF, same as a system-only pass registered on the agent
        # profile. Two narrower exceptions keep their OWN env-seeded default,
        # deliberately out of this cutover's scope (they're granular per-item
        # deploy-time convenience seeds, already fully live-controllable
        # per-item via their own ``service_config`` row, not a whole-pass
        # boot flag): ``axis:<id>`` seeds from ``PRECIS_AXES_ENABLED`` and
        # ``topic:<slug>`` from ``PRECIS_TOPICS_ENABLED`` /
        # ``PRECIS_CLASSIFY_TOPICS_ENABLED``. Skipped under ``--only`` (an
        # explicit one-pass invocation shouldn't be silently DB-gated).
        def _gate_default_on(service: str) -> bool:
            axis_default = _axis_id_default_on(service, _axes_env_set)
            if axis_default is not None:
                return axis_default
            topic_default = _topic_slug_default_on(service, _topics_env_set)
            if topic_default is not None:
                return topic_default
            if service == "classify_topics":
                from precis.workers.classify_topics import all_topic_slugs

                global_on = env_flag("PRECIS_CLASSIFY_TOPICS_ENABLED")
                return any(
                    _svc_resolver.enabled(
                        f"topic:{s}", default_on=global_on or s in _topics_env_set
                    )
                    for s in all_topic_slugs()
                )
            return _profile_default_on(service)

        _pass_gate = (
            None
            if args.only is not None
            else (
                lambda service: _svc_resolver.enabled(
                    service, default_on=_gate_default_on(service)
                )
            )
        )

        stop_flag = _install_signal_handlers()
        # gr191264: the rotation above is strictly serial (handlers then
        # ref_passes) — a single long pass (observed: an ~8-min fetch_oa
        # markup-ingest batch) starves the in-rotation heartbeat pass past
        # nursery's HOST_DARK_SILENCE_MIN, flapping a false host-dark
        # critical even though the worker is alive. A dedicated thread beats
        # independently of the rotation, bounding staleness to roughly the
        # heartbeat interval regardless of pass length; the in-rotation pass
        # above stays as a backstop (idempotent UPSERT + shared throttle
        # make a double-fire harmless). Skipped for `--once` (a one-shot
        # rotation doesn't need a background thread) and when heartbeat
        # didn't register at all (`--only <other-pass>`). Note: a live
        # `service_config` prio flip only gates the in-rotation pass above
        # (via `_pass_gate`) — NOT this thread, deliberately: the gate
        # resolver is a DB read, and heartbeat must not depend on one. This
        # is a boot-time-only gate; toggling heartbeat off via service_config
        # after boot doesn't stop an already-started thread.
        if heartbeat_registered and not args.once:
            from precis.workers.heartbeat import start_heartbeat_thread

            start_heartbeat_thread(dsn, should_stop=lambda: stop_flag["stop"])
        run_loop(
            handlers,
            store,
            batch_size=args.batch_size,
            idle_seconds=args.idle_seconds,
            once=args.once,
            should_stop=lambda: stop_flag["stop"],
            ref_passes=ref_passes,
            pass_gate=_pass_gate,
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
    the boundary instead of writing incompatible vectors.
    """
    return make_embedder(
        args.embedder,
        dim=store.embedding_dim() if store is not None else 1024,
        url=getattr(args, "embedder_url", None),
        timeout=getattr(args, "embedder_timeout", 300.0),
        max_retries=getattr(args, "embedder_max_retries", 3),
    )


def _build_handlers(
    args: argparse.Namespace, store: Store | None = None
) -> list[WorkerHandler]:
    """Materialise the handler list per ``--only``/``--profile`` flags.

    Summarize belongs to the ``system`` profile by default; ``agent`` is
    purely ref-pass driven (LLM reviewers + dream) and skips the heavy
    embedder load when unneeded. ``embed`` is manual-only as of §F cycle
    b (materializer→embed_batch→job_inproc is now the standing drain of
    the embed queue in prod — ``workers/registry.py``/
    ``workers/materialize.py``): it builds only on explicit ``--only
    embed`` (a one-off local drain, or the rollback
    ``PRECIS_MATERIALIZE_EMBED=0`` + ``precis worker --only embed``).
    ``--only`` overrides for ad-hoc invocations generally.
    """
    handlers: list[WorkerHandler] = []
    profile = getattr(args, "profile", "system")
    # §L-a: 'all' (the collapsed one-worker-per-host profile) carries the
    # system-profile handlers too — summarize must build there, or 'all'
    # isn't actually the union service_names_for_profile() claims it is.
    is_system = profile in ("system", "all")

    def _want(name: str) -> bool:
        if args.only is not None:
            return args.only == name
        if name == "embed":
            return False
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
    """Write a single ``worker: started`` row to ``worker_logs`` — the
    DB's only restart/boot signal (without it a launchd/jetsam relaunch
    loop, the incident that orphaned plan_ticks for 1.5 days, was
    invisible: the post-restart log stream looks like steady state). The
    nursery's ``worker-restart`` detector counts these rows per
    ``(host, process)``.

    A **direct, synchronous INSERT**, deliberately NOT routed through
    the buffered :class:`BufferedDBLogHandler` — that handler can drop
    the single startup record before its first flush (a size-driven
    flush during the boot log burst can demote the batch to the file
    channel before the DB connection warms). Best-effort: a failed
    insert must not block startup. A distinct human-readable line also
    goes to file/stdout (different text, so never double-counted even
    if it also reaches the DB via the handler)."""
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

    Elevates the root level to ``PRECIS_LOG_LEVEL`` (default INFO) so
    worker pass summaries land in the table even though Python's default
    root level is WARNING. Delegates to :func:`precis.utils.db_log_handler.attach`.
    """
    from precis.utils.db_log_handler import attach

    attach(dsn, level=os.environ.get("PRECIS_LOG_LEVEL", "INFO"))


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

    Also flips the process-wide drain flag (:func:`precis.liveness.
    request_drain`) so an in-flight streamed LLM call aborts between SSE
    chunks (partial salvaged, hold released) instead of running out its
    full timeout into the unit's stop-timeout SIGKILL — the graceful
    drain of self-healing-spine Layer 1, slice 2.
    """
    from precis.liveness import request_drain

    flag = {"stop": False}

    def _handler(signum: int, _frame: object) -> None:
        log.info(
            "worker: signal %d received; draining (batch ends, streams abort)",
            signum,
        )
        flag["stop"] = True
        request_drain()

    # SIGINT for interactive Ctrl-C; SIGTERM for systemd / docker
    # stop. We deliberately do NOT install SIGHUP — most operators
    # use it for "reload config" and we have no config to reload.
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return flag


__all__ = ["add_parser", "run"]
