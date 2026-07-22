"""The factory service registry — one declarative row per thing that runs.

This is the single source of truth the *factory console* + capability
scheduler are built on (docs/design/factory-console-and-scheduling.md,
slice 1). Before it, "what runs where, gated how" was spread across four
parallel lists that drifted:

1. the imperative ``if _pass_enabled("x"):`` blocks in ``cli/worker.py``,
2. the two profile ``frozenset``s in the same module,
3. the scattered ``env_flag("PRECIS_*_ENABLED")`` extra gates, and
4. a hand-maintained ``AgentSpec`` tuple in ``precis_web/routes/env.py``,
   explicitly "kept loosely aligned" with the real call sites.

A :class:`ServiceSpec` row per pass / job-type / compute service /
daemon / serving endpoint replaces all four. ``cli/worker.py`` derives
its profile membership and extra-enable gates from this table; the
``/env`` (soon ``/factory``) inspector derives its agent list from the
rows that carry an :class:`AgentIntrospect`. A totality test
(``tests/test_worker_registry.py``) AST-parses ``cli/worker.py`` and
fails CI if a wired pass has no spec — so the lists can no longer drift.

Slice 1 is a **pure refactor**: the derived profile sets and gates are
byte-identical to the literals they replace (guarded by the snapshot
test). Later slices layer capability ``requires``, live ``prio`` from
``service_config``, and the console renderer on top of the same table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ServiceKind(StrEnum):
    """What sort of runnable a :class:`ServiceSpec` describes."""

    PASS = "pass"  # a worker pass (RefPass closure or WorkerHandler)
    JOB = "job"  # a job_type drained by an executor pass
    COMPUTE = "compute"  # a heavy derived-lane service (GPU / container)
    DAEMON = "daemon"  # a standing server process (not a work unit)
    SERVING = "serving"  # an LLM/model serving endpoint


#: The worker ``--profile`` names a pass can run under in the *default*
#: rotation (no ``--only``). ``system`` = every node; ``agent`` =
#: melchior's OAuth-bearing worker. A pass in neither is cron-/env-driven.
Profile = str


@dataclass(frozen=True, slots=True)
class AgentIntrospect:
    """Deep ``claude -p`` config snapshot for the ``/env`` agent inspector.

    Only the handful of passes that dispatch through ``call_claude_agent``
    (dream, the two reviewers, the in-proc executor) carry one. Moved here
    verbatim from the old ``routes/env.py`` ``AgentSpec`` so the registry
    is the sole source; the inspector projects these fields into its page.
    """

    launchd_label: str
    model_default: str
    model_env: str
    system_prompt_env: str = ""
    directive_prompt_env: str = ""
    mcp_config_env: str = "PRECIS_MCP_CONFIG"
    disallowed_tools: tuple[str, ...] = ()
    max_turns: int = 20
    timeout_s: int = 600
    env_keys: tuple[str, ...] = ()
    gating: tuple[tuple[str, str], ...] = ()  # (env_var, why)
    wrapper: str = ""


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """One declarative row per thing the factory runs.

    ``name`` is the stable key used everywhere else (the ``--only`` /
    ``_pass_enabled`` token for passes, the ``meta.job_type`` for jobs).
    ``default_profiles`` + ``enable_env`` reproduce ``cli/worker.py``'s
    gating; ``requires`` / ``uses_model`` / ``uses_external`` /
    ``cost_sources`` feed the capability scheduler + console (later
    slices). ``introspect`` is set only for the ``claude -p`` agents.
    """

    name: str
    label: str
    category: str
    kind: ServiceKind
    one_line: str = ""
    doc_skill: str = ""
    # ── gating (passes) ──────────────────────────────────────────────
    #: worker profiles that run this pass in the default rotation.
    default_profiles: frozenset[Profile] = field(default_factory=frozenset)
    #: True when wired as a ``RefPass`` closure / handler in cli/worker.py
    #: (so the totality test knows to demand a wiring site). Daemons,
    #: serving endpoints, compute services, and job-types are False.
    ref_pass: bool = False
    #: an extra ``PRECIS_*_ENABLED`` flag that turns this pass on even
    #: outside its profile (the old inline ``or env_flag(...)`` gates).
    enable_env: str | None = None
    # ── capability + cost (feeds later slices) ──────────────────────
    requires: frozenset[str] = field(default_factory=frozenset)
    uses_model: bool = False
    uses_external: tuple[str, ...] = ()
    prompt_env: str | None = None
    cost_sources: tuple[str, ...] = ()
    #: the ``BatchResult.handler`` string this pass logs under in
    #: ``worker_logs`` when it differs from ``name`` (fetch → fetch_oa,
    #: chase → finding_chase, …). The ``/factory`` console reads
    #: last-success/last-failure by this. ``None`` → same as ``name``.
    log_name: str | None = None
    # ── the /env agent inspector (claude -p passes only) ────────────
    introspect: AgentIntrospect | None = None

    @property
    def log_handler(self) -> str:
        """The name this service's activity lands under in worker_logs."""
        return self.log_name or self.name


# ---------------------------------------------------------------------------
# The catalog. Discriminated by ``kind``; the pass rows are the ones
# ``cli/worker.py`` derives its gating from, so those must stay true.
# ---------------------------------------------------------------------------

_SYS = frozenset({"system"})
_AGT = frozenset({"agent"})

SERVICES: tuple[ServiceSpec, ...] = (
    # ── System-worker passes (every node) ───────────────────────────
    ServiceSpec(
        name="embed",
        label="Embed (bge-m3)",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        log_name="embed:bge-m3",
        one_line="Fill chunk_embeddings for chunks that lack a vector.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="summarize",
        label="Summarize (rake-lemma)",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        log_name="summarize:rake-lemma",
        one_line="Lexical RAKE keyword summary per chunk.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="chunk_keywords",
        label="Chunk keywords (KeyBERT)",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Per-chunk KeyBERT keyword arrays (F20 discovery layer).",
        doc_skill="precis-search-help",
    ),
    ServiceSpec(
        name="chase",
        label="Finding chase",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        log_name="finding_chase",
        one_line="Resolve STATUS:tracing findings (LLM hooks opt-in).",
        doc_skill="precis-search-help",
    ),
    ServiceSpec(
        name="fetch",
        label="OA fetcher (Unpaywall)",
        category="acquisition",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        log_name="fetch_oa",
        uses_external=("unpaywall",),
        one_line="Turn stub paper refs into landed OA PDFs.",
        doc_skill="precis-search-help",
    ),
    ServiceSpec(
        name="gp_fetch",
        label="Google Patents fallback",
        category="acquisition",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        log_name="fetch_google_patents",
        uses_external=("google-patents",),
        one_line="Fall-back patent fetch (PRECIS_GP_FETCH host only).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="tag_embeddings",
        label="Tag embeddings",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Embed tags so kind='tag' serves semantic discovery.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="auto_check",
        label="Auto-check evaluators",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Drain the todo-tree wait-for-condition leaves.",
        doc_skill="precis-tasks-help",
    ),
    ServiceSpec(
        name="schedule",
        label="Recurring spawner",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Mint subtasks for due level:recurring Watches.",
        doc_skill="precis-recurring-help",
    ),
    ServiceSpec(
        # §15i: the decentralized recurring-work trigger. Folds the standalone
        # launchd thin-timers (cron tick, watch poll) into the worker via a
        # DB-lease conditional advance (exactly-once, no designated node).
        # DARK — no default profile + PRECIS_SCHEDULER_ENABLED unset, so the
        # timers still own the ticks until the Phase-2 cutover flips it on.
        name="scheduler",
        label="Recurring trigger (decentralized)",
        category="jobs",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_SCHEDULER_ENABLED",
        one_line="Decentralized thin-timer cadences (cron tick, watch poll); "
        "dark until the §15i cutover.",
        doc_skill="precis-recurring-help",
    ),
    ServiceSpec(
        name="nursery",
        label="Nursery (SQL health)",
        category="health",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Surface incoherence + worker-health as kind='alert'.",
        doc_skill="precis-nursery-help",
    ),
    ServiceSpec(
        name="dispatch",
        label="Dispatch (mint jobs)",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Mint kind='job' children under executor-bearing todos.",
        doc_skill="precis-dispatch-help",
    ),
    ServiceSpec(
        name="sweeper",
        label="Sweeper (stuck jobs)",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Fail claim-orphaned running jobs so cascades unblock.",
        doc_skill="precis-job-help",
    ),
    ServiceSpec(
        name="job_coordinator",
        label="Job coordinator",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Drain long-running coordinator jobs one slice per pass.",
        doc_skill="precis-job-help",
    ),
    ServiceSpec(
        name="wake_runner",
        label="Wake runner",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Re-queue paused coordinator jobs whose wake fired.",
        doc_skill="precis-job-help",
    ),
    ServiceSpec(
        name="job_ssh_node",
        label="SSH-node executor",
        category="compute",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Drain jobs that shell out to a remote node.",
        doc_skill="precis-job-help",
    ),
    ServiceSpec(
        name="clusterize",
        label="Cluster maps (SOM)",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Hierarchical SOM cluster maps for /clusters (daily).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="corpus_reconcile",
        label="Corpus presence ledger",
        category="acquisition",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Per-host pdf_locations ledger for held-but-missing ▲.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="paper_reconcile",
        label="Paper dedup + hygiene",
        category="acquisition",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Fold duplicate paper refs + deterministic hygiene heals.",
        doc_skill="precis-overview",
    ),
    # ── Agent-worker passes (melchior / OAuth) ──────────────────────
    ServiceSpec(
        name="structural",
        label="Structural reviewer",
        category="review",
        kind=ServiceKind.PASS,
        default_profiles=_AGT,
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        cost_sources=("structural",),
        one_line="Opus 6h-dedup review of tree shape (drift, contradictions).",
        doc_skill="precis-tasks-help",
        introspect=AgentIntrospect(
            launchd_label="com.precis.worker-agent",
            model_default="claude-opus-4-8",
            model_env="PRECIS_STRUCTURAL_MODEL",
            disallowed_tools=("WebFetch", "WebSearch"),
            max_turns=12,
            timeout_s=600,
            env_keys=(
                "PRECIS_STRUCTURAL_REVIEW",
                "PRECIS_STRUCTURAL_MODEL",
                "PRECIS_MCP_CONFIG",
                "PRECIS_DATABASE_URL",
                "PRECIS_DAILY_COST_CEILING",
            ),
            gating=(
                ("PRECIS_STRUCTURAL_REVIEW", "must be '1' to run"),
                ("PRECIS_DATABASE_URL", "runtime can't load without it"),
            ),
        ),
    ),
    ServiceSpec(
        name="deep_review",
        label="Deep review",
        category="review",
        kind=ServiceKind.PASS,
        default_profiles=_AGT,
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        cost_sources=("deep_review",),
        one_line="Opus weekly Allen-style archive / prune / rebalance review.",
        doc_skill="precis-tasks-help",
        introspect=AgentIntrospect(
            launchd_label="com.precis.worker-agent",
            model_default="claude-opus-4-8",
            model_env="PRECIS_DEEP_REVIEW_MODEL",
            disallowed_tools=("WebFetch", "WebSearch"),
            max_turns=12,
            timeout_s=900,
            env_keys=(
                "PRECIS_DEEP_REVIEW",
                "PRECIS_DEEP_REVIEW_MODEL",
                "PRECIS_MCP_CONFIG",
                "PRECIS_DATABASE_URL",
                "PRECIS_DAILY_COST_CEILING",
            ),
            gating=(
                ("PRECIS_DEEP_REVIEW", "must be '1' to run"),
                ("PRECIS_DATABASE_URL", "runtime can't load without it"),
            ),
        ),
    ),
    ServiceSpec(
        name="job_claude_inproc",
        label="Claude in-process executor",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_AGT,
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        one_line="Drain claude_inproc jobs (plan_tick / fix_gripe / casts).",
        doc_skill="precis-fix-gripe-help",
        introspect=AgentIntrospect(
            launchd_label="com.precis.worker-agent",
            model_default="(per parent LLM:* tag)",
            model_env="PRECIS_JOB_CLAUDE_MODEL",
            disallowed_tools=("WebFetch", "WebSearch"),
            max_turns=20,
            timeout_s=900,
            env_keys=(
                "PRECIS_MCP_CONFIG",
                "PRECIS_DATABASE_URL",
                "PRECIS_DAILY_COST_CEILING",
                "PRECIS_FIX_REPO_DIR",
                "PRECIS_FIX_WORK_DIR",
            ),
            gating=(("PRECIS_MCP_CONFIG", "MCP config the in-proc claude reads"),),
        ),
    ),
    ServiceSpec(
        name="quota_check",
        label="Quota check (OAuth)",
        category="health",
        kind=ServiceKind.PASS,
        default_profiles=_AGT,
        ref_pass=True,
        uses_external=("anthropic",),
        one_line="Refresh the Claude OAuth utilisation snapshot; page on 401.",
        doc_skill="precis-nursery-help",
    ),
    # ── Autonomous / cron / default-off passes (no default profile) ──
    ServiceSpec(
        name="quest_dispatch",
        label="Quest allocator",
        category="jobs",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_model=True,
        one_line="Autonomous quest-loop allocator (agent profile, dark gate).",
        doc_skill="precis-quest-help",
    ),
    ServiceSpec(
        name="job_claude_docker",
        label="Sandbox executor",
        category="jobs",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_SANDBOX_ENABLED",
        requires=frozenset({"podman"}),
        one_line="Drain sandbox_run jobs in a cgroup-capped container.",
        doc_skill="precis-job-help",
    ),
    ServiceSpec(
        name="llm_summarize",
        label="LLM summarize (llm-v1)",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_SUMMARIZE_LLM",
        uses_model=True,
        cost_sources=("llm_summarize",),
        one_line="Model-authored 2-part chunk summaries (deliberate trickle).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="classify",
        label="Chunk classifier cascade",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_CLASSIFY_ENABLED",
        uses_model=True,
        cost_sources=("classify",),
        one_line="ROLE3 chunk-tag cascade (junk-gate → role3).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="llm_reconcile",
        label="LLM catalog reconcile",
        category="review",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_LLM_RECONCILE_ENABLED",
        uses_external=("openrouter",),
        one_line="Keep llm-catalog cards true vs OpenRouter; flag proxy drift.",
        doc_skill="precis-llm-help",
    ),
    ServiceSpec(
        name="paper_glossary",
        label="Paper glossary",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_PAPER_GLOSSARY_ENABLED",
        uses_model=True,
        cost_sources=("paper_glossary",),
        one_line="Per-paper inferred glossary as a card_glossary chunk.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="classify_topics",
        label="Topic-dossier classifier cascade",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_CLASSIFY_TOPICS_ENABLED",
        uses_model=True,
        cost_sources=("classify_topics",),
        one_line="Paper→topic-dossier cascade, multi-label `topic:` tags (ADR 0060).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="briefing_audio",
        label="Briefing audio (TTS)",
        category="audio",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_BRIEFING_AUDIO_ENABLED",
        requires=frozenset({"tts"}),
        one_line="Narrate the morning news briefing onto the podcast feed.",
        doc_skill="precis-audio-help",
    ),
    ServiceSpec(
        name="cast_audio",
        label="Cast audio (TTS)",
        category="audio",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_CAST_AUDIO_ENABLED",
        requires=frozenset({"tts"}),
        one_line="Narrate the daily reading-brief + nidra casts.",
        doc_skill="precis-audio-help",
    ),
    ServiceSpec(
        name="backlog_groom",
        label="Backlog groomer",
        category="jobs",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_BACKLOG_GROOM_ENABLED",
        one_line="Promote open gripes into dispatchable fix_gripe todos.",
        doc_skill="precis-fix-gripe-help",
    ),
    ServiceSpec(
        name="watch_poll",
        label="Citation-forward watcher",
        category="acquisition",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_external=("s2",),
        one_line="Poll S2 for papers citing due salient papers; mint stubs.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="news_poll",
        label="News ingestion",
        category="acquisition",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_external=("news-feeds",),
        one_line="Walk the news feed registry, mint new news refs.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        # email-kind slice 3. Dark until PRECIS_MAIL_POLL_ENABLED is set on one
        # host — no default profile, so it doesn't poll the same mailbox from
        # every node (a per-account lease that would make every-node safe is the
        # §15i scheduler, still dark). Per-account cadence + IMAP-error backoff
        # live in the pass; it fetches new bodies (BODY.PEEK) + tier-0 scans.
        name="mail_poll",
        label="Mailbox poll + tier-0 scan",
        category="acquisition",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_MAIL_POLL_ENABLED",
        uses_external=("imap",),
        one_line="Poll email accounts for new mail; inline tier-0 injection scan.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        # email slice 4: the deep rung of the injection cascade. Leases tier-0
        # verdicts (email_scan_pending_idx), re-fetches the body from IMAP,
        # scores it with a local model (+ optional tier-2 escalate), and raises
        # an alert on `high`. DARK — no default profile + PRECIS_INJECT_SCAN_
        # ENABLED unset; enabled on the agent host (melchior) where the local
        # model proxy resolves, alongside mail_poll.
        name="inject_scan",
        label="Email injection scan (tier 1/2)",
        category="acquisition",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_INJECT_SCAN_ENABLED",
        uses_model=True,
        uses_external=("imap",),
        cost_sources=("inject_scan",),
        one_line="Model-score flagged email for prompt injection; quarantine on high.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="briefing",
        label="Morning briefing",
        category="review",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_model=True,
        one_line="Summarize recent news refs into a dated digest.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="dream_agent",
        label="Dream agent",
        category="review",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        prompt_env="PRECIS_DREAM_PROMPT_PATH",
        one_line="15-min reflective memory pass (own cadence, PRECIS_DREAM_AGENT).",
        doc_skill="precis-overview",
        introspect=AgentIntrospect(
            launchd_label="com.precis.dream",
            model_default="claude-opus-4-8",
            model_env="PRECIS_DREAM_AGENT_MODEL",
            system_prompt_env="PRECIS_DREAM_SOUL_PATH",
            directive_prompt_env="PRECIS_DREAM_PROMPT_PATH",
            disallowed_tools=("WebFetch", "WebSearch"),
            max_turns=20,
            timeout_s=600,
            env_keys=(
                "PRECIS_DREAM_AGENT",
                "PRECIS_DREAM_AGENT_MODEL",
                "PRECIS_DREAM_PROMPT_PATH",
                "PRECIS_DREAM_SOUL_PATH",
                "PRECIS_MCP_CONFIG",
                "PRECIS_DATABASE_URL",
                "PRECIS_PROCESS",
            ),
            gating=(
                ("PRECIS_DREAM_AGENT", "must be '1' / 'true' to run"),
                ("PRECIS_DATABASE_URL", "runtime can't load without it"),
            ),
            wrapper="/opt/asa/bin/dream-pass.sh",
        ),
    ),
    # ── Standalone daemons (separate processes, not gate flags) ─────
    ServiceSpec(
        name="embedder",
        label="Embedder (serve-embeddings)",
        category="daemon",
        kind=ServiceKind.DAEMON,
        one_line="Per-host bge-m3 model server the worker calls (/readyz).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="web",
        label="Web UI (uvicorn)",
        category="daemon",
        kind=ServiceKind.DAEMON,
        one_line="The precis_web browser UI (melchior).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="asa_bot",
        label="asa-bot (Discord)",
        category="daemon",
        kind=ServiceKind.DAEMON,
        uses_external=("discord",),
        one_line="The Discord bridge — our only Discord interface.",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="watch",
        label="Paper ingestor (watch)",
        category="daemon",
        kind=ServiceKind.DAEMON,
        one_line="Inline PDF-inbox ingestor (precis_add).",
        doc_skill="precis-overview",
    ),
    # ── LLM serving endpoint ────────────────────────────────────────
    ServiceSpec(
        name="llama_swap",
        label="llama-swap (VRAM swapper)",
        category="serving",
        kind=ServiceKind.SERVING,
        one_line="Per-node VRAM model-swapper serving local inference.",
        doc_skill="precis-llm-help",
    ),
    # ── Compute services (derived lane; GPU / container per job) ─────
    ServiceSpec(
        name="struct_relax",
        label="Structure relax (DFT/MLP)",
        category="compute",
        kind=ServiceKind.COMPUTE,
        requires=frozenset({"gpu"}),
        one_line="GPAW / ML-potential relax on a GPU node.",
        doc_skill="precis-structure-help",
    ),
    ServiceSpec(
        name="fold",
        label="AlphaFold3 fold",
        category="compute",
        kind=ServiceKind.COMPUTE,
        requires=frozenset({"gpu"}),
        one_line="AlphaFold3 structure prediction on spark.",
        doc_skill="precis-overview",
    ),
)

#: Fast lookup by ``name``.
SERVICES_BY_NAME: dict[str, ServiceSpec] = {s.name: s for s in SERVICES}


def service_names_for_profile(profile: Profile) -> frozenset[str]:
    """Pass names that run in ``profile``'s default rotation.

    ``cli/worker.py`` derives its ``system_passes`` / ``agent_passes``
    from this instead of the old hand-written ``frozenset`` literals.
    """
    return frozenset(s.name for s in SERVICES if profile in s.default_profiles)


def enable_env_for(name: str) -> str | None:
    """The extra ``PRECIS_*_ENABLED`` flag for pass ``name``, if any."""
    spec = SERVICES_BY_NAME.get(name)
    return spec.enable_env if spec is not None else None


def agent_specs() -> tuple[ServiceSpec, ...]:
    """The passes that carry an :class:`AgentIntrospect` (the /env list)."""
    return tuple(s for s in SERVICES if s.introspect is not None)


__all__ = [
    "SERVICES",
    "SERVICES_BY_NAME",
    "AgentIntrospect",
    "Profile",
    "ServiceKind",
    "ServiceSpec",
    "agent_specs",
    "enable_env_for",
    "service_names_for_profile",
]
