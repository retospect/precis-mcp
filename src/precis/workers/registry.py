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

from precis.utils.llm.router import Tier


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
    #: Fallback display string when ``model_env`` isn't set in the agent's
    #: effective env — either a free-form description (e.g.
    #: ``job_claude_inproc``'s ``"(per parent meta.llm_tier)"``) or, for a
    #: row that always resolves through one fixed router tier, left ``""``
    #: in favor of ``tier_default`` below.
    model_default: str
    model_env: str
    #: The router :class:`~precis.utils.llm.router.Tier` this agent's model
    #: resolves through when unset in its effective env — the ``/env``
    #: inspector calls :func:`~precis.utils.llm.router.resolve_model` on it
    #: at render time (rather than baking a model id in here) so the page
    #: reflects a live ``app_settings`` override too, not just the compiled
    #: default. ``None`` ⇒ use ``model_default`` verbatim (a row with no
    #: single fixed tier, e.g. ``job_claude_inproc``).
    tier_default: Tier | None = None
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
    ``_register`` / ``service_config.service`` token for passes, the
    ``meta.job_type`` for jobs). ``default_profiles`` feeds ``_register``'s
    (``cli/worker.py``) structural registration decision; ``enable_env`` is
    now (§L control cutover) consulted only by the deploy-time seed task —
    the live default a missing ``service_config`` row falls back to is
    ``name in profile_passes`` alone, no env read.  ``requires`` /
    ``uses_model`` / ``uses_external`` / ``cost_sources`` feed the capability
    scheduler + console (later slices). ``introspect`` is set only for the
    ``claude -p`` agents.
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
    #: presence marks this a formerly-env-gated pass (always registers,
    #: no ``default_profiles`` needed — see ``_register``/``_should_register``
    #: in cli/worker.py). §L control cutover: the named ``PRECIS_*_ENABLED``
    #: var itself is retired as the LIVE gate — a ``service_config`` row is
    #: the only thing that turns the pass on now — but it's still the name
    #: the deploy-time seed task (``deploy/roles/precis_worker*``) mirrors
    #: into a row from today's plist flag state, and quest_loop_reconcile's
    #: registration-vs-gate-default alignment test still pins it.
    enable_env: str | None = None
    #: env vars that must ALL be set non-empty on this host for the pass to
    #: default ON when profile membership alone says yes (gr193672: under
    #: ``--profile all`` the union carries the ``_AGT``-only passes onto
    #: every host, and one with no gate defaulted ON on hosts that cannot
    #: run it — claude_inproc plan ticks hard-failed wherever the claude
    #: CLI / MCP config is absent). Consulted by ``_profile_default_on``
    #: (cli/worker.py) as the no-row baseline only — an explicit
    #: ``service_config`` row still overrides in either direction.
    capability_env: tuple[str, ...] = ()
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
        # §F cycle b cutover: the materializer (workers/materialize.py) →
        # embed_batch → job_inproc path is now the standing drain of the
        # embed queue — NOT `default_profiles`, so this registration is
        # for a manual `--only embed` run only (still useful for a
        # one-off local drain / the rollback story). EmbedHandler and its
        # machinery stay; embed_batch reuses them directly.
        name="embed",
        label="Embed (bge-m3)",
        category="discovery",
        kind=ServiceKind.PASS,
        log_name="embed:bge-m3",
        one_line="Fill chunk_embeddings for chunks that lack a vector "
        "(manual/rollback only — materializer drains this in prod).",
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
        # docs/proposals/citation-bib-parse.md: parses each held paper's
        # bibliography (numeric-bracket entries) into `paper_bib_entries`
        # rows and matches each to a DOI (local `s2_neighbors` exact match,
        # else a Crossref bibliographic query) + `held_ref_id`. Default-ON
        # like `chunk_keywords`/`fetch` above — the `meta.bib_parse_version`
        # predicate converges (a claimed paper is stamped and never
        # re-claimed at the same version) so normal cadence drains the
        # backlog gradually; `--only bib_parse` is the fast-path burst.
        name="bib_parse",
        label="Bibliography parse + match",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        uses_model=True,
        uses_external=("crossref",),
        cost_sources=("bib_parse",),
        one_line="Parse each paper's bibliography into paper_bib_entries, "
        "DOI-matched (local s2_neighbors, else Crossref).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        # docs/proposals/citation-taproot-resolve.md: extracts inline
        # citation markers ([126], [129,130], <sup>-wrapped) from paper
        # body chunks into `chunk_citations`, keyed to the parsed
        # `paper_bib_entries` marker (false-positive guarded — only real
        # bib markers accepted). Default-ON like `bib_parse`; a
        # `BIBMARK:<version>` chunk tag converges the sweep so normal
        # cadence drains the backlog; `--only bib_mark` is the burst. Pure
        # regex — no model, no external call.
        name="bib_mark",
        label="Inline citation-marker extraction",
        category="discovery",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        cost_sources=("bib_mark",),
        one_line="Extract inline [N] markers into chunk_citations "
        "(taproot.resolve_citation).",
        doc_skill="precis-citation-help",
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
        one_line="Mint subtasks for due recurring Watches.",
        doc_skill="precis-recurring-help",
    ),
    ServiceSpec(
        # §15i/§A: the decentralized recurring-work trigger. Folds the
        # standalone launchd thin-timers (cron tick, watch poll, dream,
        # anki-sync) into the worker via a DB-lease conditional advance
        # (exactly-once, no designated node). LIVE on both profiles — the
        # agent profile must run it too, or a host-pinned cadence
        # (dream_agent, anki_sync — both melchior) never has an eligible
        # claimant.
        name="scheduler",
        label="Recurring trigger (decentralized)",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS | _AGT,
        ref_pass=True,
        one_line="Decentralized cadences: cron tick, watch poll, dream_agent, "
        "anki_sync (host-pinned melchior).",
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
        # §D (docs/proposals/health-watchdog.md). Fleet-singleton via the
        # `health_digest` scheduler cadence (workers/scheduler.py, hourly,
        # host-agnostic) — NOT `default_profiles`, mirroring `dream_agent`
        # below: a cadence-fired pass registers here only for a manual
        # `--only health_digest` run; the standing trigger is the cadence's
        # lease claim, so it does not also run every idle cycle on every
        # system-profile host (that would need its own duplicate throttle,
        # which §A's lease machinery already is).
        name="health_digest",
        label="Health digest (§D liveness net)",
        category="health",
        kind=ServiceKind.PASS,
        ref_pass=True,
        one_line="Periodic outcome-based liveness digest — Layer-1 curated "
        "checks + cadence staleness + Layer-2 registry coherence, pushed "
        "daily/on-degradation; pure template, no LLM.",
        doc_skill="precis-health-digest-help",
    ),
    ServiceSpec(
        # §F cycle a (docs/proposals/cluster-scheduling.md §F). Fleet-
        # singleton via the `materialize` scheduler cadence (workers/
        # scheduler.py, 300s) — same shape as `health_digest` just above:
        # NOT `default_profiles`, so this registration is for a manual
        # `--only materialize` run only; the standing trigger is the
        # cadence's lease claim. DARK unless PRECIS_MATERIALIZE_EMBED=1 —
        # see workers/materialize.py's module docstring.
        name="materialize",
        label="Demand materializer (§F)",
        category="jobs",
        kind=ServiceKind.PASS,
        ref_pass=True,
        one_line="Mints bounded embed_batch jobs above the backlog "
        "high-water mark; dark until PRECIS_MATERIALIZE_EMBED=1.",
        doc_skill="precis-job-help",
    ),
    ServiceSpec(
        # §A: the collection+upsert core moved to workers/heartbeat.py so it
        # can run as a per-host pass too, not just the manual/timer-fired
        # `precis heartbeat` CLI. Deliberately NOT on scheduler_leases —
        # heartbeat is the liveness signal that lease/claim machinery is
        # judged by. In-process self-throttled (PRECIS_HEARTBEAT_INTERVAL_
        # SECONDS, default 60s); a double-fire against the still-live
        # launchd/systemd timer (retired only in §L) is a harmless upsert.
        name="heartbeat",
        label="Heartbeat (load + temp)",
        category="health",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Per-host liveness UPSERT (host_heartbeat); self-throttled, "
        "timers pending §L.",
        doc_skill="precis-overview",
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
        # §F cycle a: the generic in-proc lane for a BOUNDED job_type
        # (embed_batch is the first) that also needs slot reservation
        # (respect_reserve=True) — see workers/executors/job_inproc.py.
        name="job_inproc",
        label="In-process job executor (bounded, generic)",
        category="jobs",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Drain bounded in-proc job_types one-per-tick "
        "(embed_batch is the first).",
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
    ServiceSpec(
        name="openalex_enrich",
        label="OpenAlex abstract/metadata enrich",
        category="acquisition",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        uses_external=("openalex",),
        one_line="Fill missing top-level abstracts + OpenAlex metadata on a "
        "cadence (self-healing).",
        doc_skill="precis-overview",
    ),
    # ── Agent-worker passes (melchior / OAuth) ──────────────────────
    ServiceSpec(
        # gr192752: cadence-fired via the `structural` scheduler lease
        # (workers/scheduler.py) — NOT `default_profiles`, mirroring
        # `health_digest`/`dream_agent`: registering into every rotation
        # would need its own duplicate throttle, which §A's lease
        # machinery already is. The lease is fleet-wide and the env gate
        # is live on TWO hosts (gateway + inference), so one host's
        # wedged rotation (chase monopolizing the strictly-serial
        # `--profile all` loop) can no longer starve this reviewer — the
        # other eligible host's scheduler pass wins the fire.
        name="structural",
        label="Structural reviewer",
        category="review",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        cost_sources=("structural",),
        one_line="Opus 6h-dedup review of tree shape (drift, contradictions) "
        "— `structural` scheduler cadence (gr192752), not a profile default.",
        doc_skill="precis-tasks-help",
        introspect=AgentIntrospect(
            launchd_label="com.precis.worker",
            model_default="",
            tier_default=Tier.BIG,
            model_env="PRECIS_STRUCTURAL_MODEL",
            disallowed_tools=("WebFetch", "WebSearch"),
            max_turns=30,
            timeout_s=900,
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
        # gr192752: same shape as `structural` just above — cadence-fired
        # via the `deep_review` scheduler lease, not `default_profiles`.
        # See that comment for the full rationale (cross-host failover is
        # the fix, not an in-band reorder of the rotation).
        name="deep_review",
        label="Deep review",
        category="review",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        cost_sources=("deep_review",),
        one_line="Opus weekly Allen-style archive / prune / rebalance review "
        "— `deep_review` scheduler cadence (gr192752), not a profile default.",
        doc_skill="precis-tasks-help",
        introspect=AgentIntrospect(
            launchd_label="com.precis.worker",
            model_default="",
            tier_default=Tier.BIG,
            model_env="PRECIS_DEEP_REVIEW_MODEL",
            disallowed_tools=("WebFetch", "WebSearch"),
            max_turns=60,
            timeout_s=1800,
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
        capability_env=("PRECIS_MCP_CONFIG",),
        ref_pass=True,
        uses_model=True,
        uses_external=("anthropic",),
        one_line="Drain claude_inproc jobs (plan_tick / fix_gripe / casts).",
        doc_skill="precis-fix-gripe-help",
        introspect=AgentIntrospect(
            launchd_label="com.precis.worker",
            model_default="(per parent meta.llm_tier)",
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
        # PRECIS_MCP_CONFIG is a proxy: the real dependency is the claude
        # CLI + OAuth creds, but 20b deploys that whole agent env bundle
        # only to the gateway group, so its presence IS the deploy-
        # controlled "claude-capable host" marker.
        capability_env=("PRECIS_MCP_CONFIG",),
        ref_pass=True,
        uses_external=("anthropic",),
        one_line="Refresh the Claude OAuth utilisation snapshot; page on 401.",
        doc_skill="precis-nursery-help",
    ),
    ServiceSpec(
        name="disk_check",
        label="Disk-space watch",
        category="health",
        kind=ServiceKind.PASS,
        default_profiles=_SYS,
        ref_pass=True,
        one_line="Raise an alert before a node's disk fills (gripe 191008).",
        doc_skill="precis-nursery-help",
    ),
    # ── Autonomous / cron / default-off passes (no default profile) ──
    ServiceSpec(
        name="quest_loop_reconcile",
        label="Quest loop reconciler",
        category="jobs",
        kind=ServiceKind.PASS,
        ref_pass=True,
        uses_model=True,
        # The pass registers via a direct `quest_loop_enabled()` env check
        # (cli/worker.py), but the per-cycle `pass_gate` derives its default from
        # this spec's `enable_env`. Without it the two gates DISAGREE: the pass
        # registers yet is skipped every cycle (default_on=False), which is
        # exactly how it silently went dark when `e69c2b06` switched the gate
        # default from blanket-true to `_env_profile_default_on`. Keep this in
        # lockstep with the registration env var.
        enable_env="PRECIS_QUEST_LOOP_ENABLED",
        one_line=(
            "Ensures each active quest has one live quest_tick coordinator "
            "loop, re-arming a rested one (agent profile, dark gate)."
        ),
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
        # §F cycle a: the first `requires`-carrying job_type (not a pass/
        # compute-lane row) — `effective_requires` derives {'embedder': 1}
        # from THIS row's `requires`, matching struct_relax/fold's
        # gpu-derivation shape. `kind=ServiceKind.JOB` (a job_type drained
        # by an executor pass), not PASS/COMPUTE: `job_inproc` is the
        # executor pass that drains it, registered separately above.
        name="embed_batch",
        label="Embed batch (job)",
        category="jobs",
        kind=ServiceKind.JOB,
        requires=frozenset({"embedder"}),
        one_line="Bounded work order draining the derived embed queue "
        "(ADR 0007) — minted by the materialize cadence, dark by default.",
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
        # gripe 196447 Layer 2: corpus remediation for bibliography chunks
        # mis-typed `chunk_kind='paragraph'` (marker/PDF-OCR ingest) — retypes
        # the content-detected ones to `references` in place and deletes their
        # stale chunk_embeddings/chunk_summaries so they drop out of semantic
        # search. MUTATES existing corpus, so DEFAULT-OFF (no `default_profiles`,
        # `enable_env` gate) — never auto-runs on deploy; invoke via
        # `--only bib_retag`. Converges on `meta.bib_retag_version`. No LLM, no
        # external call — pure regex detection (shared with `bib_parse`).
        name="bib_retag",
        label="Bibliography retag (Layer-2 remediation)",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_BIB_RETAG_ENABLED",
        cost_sources=("bib_retag",),
        one_line="Retype mis-typed 'paragraph' bibliography chunks to "
        "'references' + drop their embeddings (gripe 196447, manual).",
        doc_skill="precis-overview",
    ),
    ServiceSpec(
        name="inbound_chase",
        label="Inbound citation chase",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_INBOUND_CHASE_ENABLED",
        uses_model=True,
        uses_external=("s2",),
        cost_sources=("inbound_chase",),
        one_line=(
            "Exhaustive one-hop inbound citer sweep + chunk-level cites "
            "verdicts for activated papers (citation-chunk-grounding)."
        ),
        doc_skill="precis-search-help",
    ),
    ServiceSpec(
        name="hub_refine",
        label="Taproot hub refine",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_TAPROOT_REFINE_ENABLED",
        uses_model=True,
        cost_sources=("hub_refine",),
        one_line=(
            "Periodic, converging enrichment of existing taproot claim "
            "hubs with corpus corroborators."
        ),
        doc_skill="precis-taproot-help",
    ),
    ServiceSpec(
        name="chase_trigger",
        label="Taproot chase trigger",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_TAPROOT_CHASE_TRIGGER_ENABLED",
        cost_sources=("chase_trigger",),
        one_line=(
            "Marks a claim hub TAPROOT_DUE when a freshly-embedded near "
            "paper chunk lands, so hub_refine refreshes it promptly "
            "instead of waiting out the backstop."
        ),
        doc_skill="precis-taproot-help",
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
        name="axis",
        label="Axis classifier (generic)",
        category="discovery",
        kind=ServiceKind.PASS,
        ref_pass=True,
        enable_env="PRECIS_AXES_ENABLED",
        uses_model=True,
        cost_sources=("axis",),
        one_line=(
            "Generic data/axes/<id>.yaml classifier w/ prereq enforcement "
            "(ADR 0047 §3); PRECIS_AXES_ENABLED=<comma ids> picks which run."
        ),
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
        one_line="Reflective memory pass — §A scheduler-lease cadence "
        "(host-pinned melchior, PRECIS_DREAM_AGENT), default 15-min knob.",
        doc_skill="precis-overview",
        introspect=AgentIntrospect(
            # §A: the standalone com.precis.dream LaunchDaemon (dream-pass.sh)
            # is retired — dream_agent's trigger is now the `dream_agent`
            # scheduler cadence, running inside com.precis.worker (post-§L-b
            # collapsed unit; the old com.precis.worker-agent label is stale).
            launchd_label="com.precis.worker",
            model_default="",
            tier_default=Tier.BIG,
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
            # No wrapper script (§A) — dream-pass.sh is retired; the
            # com.precis.worker-agent plist sets the env directly.
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

    ``"all"`` (§L-a collapsed-worker enablement, one-worker-per-host) is
    not a real ``default_profiles`` member on any :class:`ServiceSpec` —
    it's the union of the ``system`` and ``agent`` rotations, for the
    single collapsed worker that eventually replaces both split units on
    a host. Handled here, not at call sites, so ``--profile all`` reads
    like any other profile everywhere else in ``cli/worker.py``.
    """
    if profile == "all":
        return service_names_for_profile("system") | service_names_for_profile("agent")
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
