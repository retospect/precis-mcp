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

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.cli._worker_passes import build_ref_passes
from precis.embedder import make_embedder
from precis.format import serialize
from precis.store import Store
from precis.utils.env import env_flag
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
    only: list[str] | None, name: str, *, profile_passes: frozenset[str] = frozenset()
) -> bool:
    """Whether ``name`` registers into ``ref_passes`` on this invocation.

    §L control cutover: registration is now PURELY structural — never
    consults ``service_config`` (see the module's THE GAP note: a
    stale/absent row at boot used to keep a pass out of ``ref_passes``
    until restart, so a later live prio flip had nothing to gate). The
    per-cycle ``pass_gate`` (TTL-cached, consulted every cycle) is now
    the ONE decision point for whether a registered pass actually *runs*.

    ``only`` (``--only X``, repeatable) forces exactly the named
    pass(es) regardless of profile membership. Otherwise always-register
    cases: a pass in this worker's profile rotation (``name in
    profile_passes`` — tied to ``--profile``, restart-time); a
    formerly-``PRECIS_*_ENABLED``-gated pass (registry's ``enable_env``);
    an ``axis:<id>`` pseudo-service; a name with NO ``ServiceSpec`` at
    all (a plugin factory's own pass — it already gated its own
    eligibility). A pass with neither profile membership nor
    ``enable_env`` does not register.
    """
    if only is not None:
        return name in only
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
    only: list[str] | None,
    global_on: bool,
    topics_env: frozenset[str],
    slugs: list[str],
) -> list[str] | None:
    """Enabled topic slugs for the ``classify_topics`` pass, or ``None`` for
    the full taxonomy.

    ``--only classify_topics`` (repeatable ``--only``, so this checks
    membership) and ``PRECIS_CLASSIFY_TOPICS_ENABLED=1`` are the admin
    full-backfill hatches (preserved the pre-0068 meaning): the former
    always sweeps every topic (a single-pass, node-targeted invocation
    shouldn't be silently narrowed by per-topic gates); the latter is the
    legacy "all topics default-on" env seed, still refinable per-topic by
    a ``service_config`` row or ``PRECIS_TOPICS_ENABLED``.
    """
    if only is not None and "classify_topics" in only:
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
# minutes each cycle while ``minter`` (which mints the planner's
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
# The bands are an explicit :class:`PassPriority` enum rather than bare
# ints so the intent reads at a glance, and every key here is guarded
# by ``test_ref_pass_priority_keys_match_registered_passes``: it parses
# this module's AST and fails if a key no longer matches a live
# ``ref_passes.append(_<name>_pass)`` site — so renaming a band-assigned
# closure can no longer *silently* drop it into the DEFAULT band.


class PassPriority(IntEnum):
    """Scheduling band for a ref-pass; lower runs earlier each cycle."""

    JOB = 0  # job execution: plan_tick coroutine + other job runners
    PLANNER = 10  # planner lifecycle: unblock, schedule, mint, unstick
    HEALTH = 15  # cheap SQL health
    DEFAULT = 20  # enrichment / indexing / reconcile / plugins
    BACKGROUND = 30  # heavy tail: external fetch + LLM + reviewers


_REF_PASS_PRIORITY: dict[str, PassPriority] = {
    "_job_claude_inproc_pass": PassPriority.JOB,
    "_job_coordinator_pass": PassPriority.JOB,
    "_job_ssh_node_pass": PassPriority.JOB,
    "_job_inproc_pass": PassPriority.JOB,
    "_job_claude_docker_pass": PassPriority.JOB,
    "_wake_runner_pass": PassPriority.JOB,
    "_auto_check_pass": PassPriority.PLANNER,
    "_schedule_pass": PassPriority.PLANNER,
    "_scheduler_pass": PassPriority.PLANNER,
    "_minter_pass": PassPriority.PLANNER,
    "_sweeper_pass": PassPriority.PLANNER,
    "_nursery_pass": PassPriority.HEALTH,
    "_heartbeat_pass": PassPriority.HEALTH,
    "_chase_pass": PassPriority.BACKGROUND,
    "_bib_parse_pass": PassPriority.BACKGROUND,
    "_bib_retag_pass": PassPriority.DEFAULT,
    "_inbound_chase_pass": PassPriority.BACKGROUND,
    "_hub_refine_pass": PassPriority.BACKGROUND,
    "_hub_tagline_pass": PassPriority.BACKGROUND,
    "_conflict_search_pass": PassPriority.BACKGROUND,
    "_chase_trigger_pass": PassPriority.BACKGROUND,
    "_inbound_ground_pass": PassPriority.BACKGROUND,
    "_fetch_pass": PassPriority.BACKGROUND,
    "_gp_fetch_pass": PassPriority.BACKGROUND,
    "_stub_rank_pass": PassPriority.BACKGROUND,
    "_llm_summarize_pass": PassPriority.BACKGROUND,
    "_classify_pass": PassPriority.BACKGROUND,
    "_llm_reconcile_pass": PassPriority.BACKGROUND,
    "_paper_glossary_pass": PassPriority.BACKGROUND,
    "_context_sentence_pass": PassPriority.BACKGROUND,
    "_paper_rank_pass": PassPriority.BACKGROUND,
    "_classify_topics_pass": PassPriority.BACKGROUND,
    "_axis_pass": PassPriority.BACKGROUND,
    "_mail_poll_pass": PassPriority.BACKGROUND,
    "_inject_scan_pass": PassPriority.BACKGROUND,
    "_structural_pass": PassPriority.BACKGROUND,
    "_deep_review_pass": PassPriority.BACKGROUND,
    "_dream_agent_pass": PassPriority.BACKGROUND,
}


def _ref_pass_priority(fn: RefPass) -> PassPriority:
    """Scheduling band for a ref-pass closure (lower runs earlier)."""
    return _REF_PASS_PRIORITY.get(getattr(fn, "__name__", ""), PassPriority.DEFAULT)


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
        "schedule, nursery, minter, sweeper. 'agent' = the OAuth "
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
            "chase_trigger",
            "inbound_ground",
            "fetch",
            "gp_fetch",
            "stub_rank",
            "tag_embeddings",
            "job_claude_inproc",
            "job_inproc",
            "job_ssh_node",
            "job_claude_docker",
            "dream_agent",
            "auto_check",
            "schedule",
            "nursery",
            "heartbeat",
            "structural",
            "deep_review",
            "minter",
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
        action="append",
        default=None,
        help="Restrict to one or more handler kinds (repeatable: "
        "`--only job_claude_inproc --only job_inproc` runs exactly "
        "those two passes). Overrides --profile when set. Useful for "
        "ad-hoc backfills (`--only embed --once`), debugging, and a "
        "dedicated agent-lane unit that must run more than one pass "
        "without picking up the rest of a profile's rotation.",
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
    p.add_argument(
        "--migrate",
        action="store_true",
        help="Apply pending DB migrations at startup instead of refusing "
        "when the schema is behind (same as PRECIS_AUTO_MIGRATE=1).",
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
    # Startup schema gate: refuse (or self-heal under --migrate /
    # PRECIS_AUTO_MIGRATE) when the packaged migrations are ahead of this
    # DB — before opening the pool.
    from precis.store.migrate import check_schema_at_startup

    check_schema_at_startup(dsn, auto_migrate=True if args.migrate else None)
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
        ref_passes, heartbeat_registered, _axes_env_set, _topics_env_set = (
            build_ref_passes(args, store, handlers, _register, _svc_resolver)
        )

        # Real work before background I/O. The run loop is sequential
        # per cycle, so ordering is priority: job execution + planner
        # lifecycle must run ahead of slow fetch/enrichment/reviewer
        # passes or a fetch backlog starves ``minter`` and the
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


def _build_chase_pass(
    args: argparse.Namespace, store: Store, handlers: list[WorkerHandler]
) -> RefPass:
    """Build the finding-chase pass — same sibling-worker pattern as the
    other ``ref_passes`` entries, but for STATUS:tracing/acquiring
    findings. Default-off LLM hooks via ``--with-llm`` or
    ``PRECIS_CHASE_LLM=1``. The sibling-vs-base-class rationale lives in
    the ``precis.workers`` package docstring.

    Extracted from :func:`run` (rather than inlined) so the env/argparse
    seam that decides ``with_llm`` — and the fact that the pass closure
    below actually *uses* that decision rather than re-reading
    ``args.with_llm`` (always ``False`` in production; no
    playbook/plist ever passes ``--with-llm``) — is directly unit-
    testable without booting the whole worker.
    """
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
    _chase_taproot_flag_on = bool(int(os.environ.get(_TAPROOT_CHASE_ENV, "0") or "0"))
    _chase_with_llm = args.with_llm or env_flag("PRECIS_CHASE_LLM")
    if _chase_embed_handler is not None:
        chase_embedder = _chase_embed_handler.embedder
    elif _chase_taproot_flag_on and _chase_with_llm:
        try:
            chase_embedder = _resolve_embedder(args, store)
        except Exception:
            log.warning(
                "chase: embedder unavailable -- taproot bridge will degrade to no-op",
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
            with_llm=_chase_with_llm,
            taproot_embedder=chase_embedder,
        )
        return _BatchResult(
            handler="finding_chase",
            claimed=r["claimed"],
            ok=r["ok"],
            failed=r["failed"],
        )

    return _chase_pass


def _build_chase_trigger_pass(
    args: argparse.Namespace, store: Store, handlers: list[WorkerHandler]
) -> RefPass:
    """Build the chase_trigger pass — the incremental counterpart to
    hub_refine (transient-napping-parrot Phase 1): sweeps freshly-embedded
    paper/patent chunks against the (tiny) claim-embedding index and marks a
    near claim hub TAPROOT_DUE, so hub_refine's due-set claim query picks it
    up promptly instead of waiting out its 90d backstop. Default-OFF — dark
    like every other taproot service (§L: ``service prio`` controls it now,
    no ``PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED``). Needs an embedder (both to
    embed claim sentences and to compare chunk vectors against them); same
    reuse-the-booted-EmbedHandler-or-construct-fresh-LAZILY pattern as
    hub_refine (register-all means this closure is built on every profile, so
    the fresh-embedder fallback must not fire until the pass first actually
    runs), same embedder-unavailable no-op degrade.

    Extracted from :func:`run` (rather than inlined) so the seam that decides
    chase_trigger's batch size — and the fact that the pass closure below does
    NOT forward the shared loop ``batch_size`` — is directly unit-testable
    without booting the whole worker (mirrors :func:`_build_chase_pass`).
    """
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
                    "chase_trigger: embedder unavailable -- pass will degrade to no-op",
                    exc_info=True,
                )
                embedder = None
        _chase_trigger_embedder_cache.append(embedder)
        return embedder

    def _chase_trigger_pass(batch_size: int) -> _ChaseTriggerBatchResult:
        # Do NOT forward the shared loop ``batch_size`` (the generic
        # ``--batch-size`` flag, default 32) — that made chase_trigger's own
        # ``PRECIS_TAPROOT_CHASE_TRIGGER_BATCH_SIZE`` knob (default 200) dead
        # code in prod: the loop's concrete 32 always overrode
        # ``run_chase_trigger_pass``'s ``None``-fallback to
        # ``_batch_size_default()``, so it claimed 32 chunks/pass instead of
        # 200 and drained a 3.15M backlog at ~1k chunks/day. Same argparse-
        # default-overrides-env-fallback trap as gr342562's ``with_llm``.
        # Pass ``None`` so the pass's own env default governs, decoupled from
        # the generic flag (mirrors classify's dedicated batch knob).
        r = run_chase_trigger_pass(
            store,
            embedder=_chase_trigger_get_embedder(),
            batch_size=None,
        )
        # chase_trigger's own return shape ({claim_embeds, chunks_swept,
        # due_marked, failed}) doesn't carry a {claimed, ok, failed} triple.
        # Count BOTH chunks swept and claim-embeddings refreshed as work
        # units, so a cycle that only refreshed stale claim vectors
        # (chunks_swept=0) still reports claimed>0 and isn't misread as idle
        # by the runner (runner.py: claimed>0 => any_work => no idle backoff).
        _worked = r["chunks_swept"] + r["claim_embeds"]
        return _ChaseTriggerBatchResult(
            handler="chase_trigger",
            claimed=_worked + r["failed"],
            ok=_worked,
            failed=r["failed"],
        )

    return _chase_trigger_pass


def _build_inbound_ground_pass(
    args: argparse.Namespace, store: Store, handlers: list[WorkerHandler]
) -> RefPass:
    """Build the inbound_ground pass (``workers/inbound_ground.py``). Mirrors
    :func:`_build_chase_trigger_pass`: lazy embedder resolution with the
    no-op degrade, and the shared loop ``batch_size`` is NOT forwarded — the
    pass's own ``PRECIS_INBOUND_GROUND_PAPERS_PER_PASS`` knob governs how
    many papers (each up to ``PRECIS_INBOUND_GROUND_MAX_LLM`` verifier
    calls) one cycle grounds, so the generic ``--batch-size`` default can't
    silently multiply LLM spend.
    """
    from precis.workers.embed import EmbedHandler as _InboundGroundEmbedHandler
    from precis.workers.inbound_ground import run_inbound_ground_pass
    from precis.workers.runner import BatchResult as _InboundGroundBatchResult

    _inbound_ground_embed_handler = next(
        (h for h in handlers if isinstance(h, _InboundGroundEmbedHandler)), None
    )
    _inbound_ground_embedder_cache: list[Any] = []

    def _inbound_ground_get_embedder() -> Any:
        if _inbound_ground_embedder_cache:
            return _inbound_ground_embedder_cache[0]
        if _inbound_ground_embed_handler is not None:
            embedder = _inbound_ground_embed_handler.embedder
        else:
            try:
                embedder = _resolve_embedder(args, store)
            except Exception:
                log.warning(
                    "inbound_ground: embedder unavailable -- pass will degrade to no-op",
                    exc_info=True,
                )
                embedder = None
        _inbound_ground_embedder_cache.append(embedder)
        return embedder

    def _inbound_ground_pass(batch_size: int) -> _InboundGroundBatchResult:
        r = run_inbound_ground_pass(
            store, embedder=_inbound_ground_get_embedder(), limit=None
        )
        return _InboundGroundBatchResult(
            handler="inbound_ground",
            claimed=r["claimed"],
            ok=r["ok"],
            failed=r["failed"],
        )

    return _inbound_ground_pass


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
            return name in args.only
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
