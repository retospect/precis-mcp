"""The alert failure-id registry — every rule ``raise_alert`` can emit.

``raise_alert`` dedups on ``(alert_source, fingerprint)`` and that pair
*is* the failure id — but until this module existed it was implicit:
f-string conventions scattered across a dozen producers, nothing
enumerable, nothing an agent could ask about by name. This is the
catalogue (``docs/backlog/alert-failure-id-registry.md``; the model is
PCB DRC's stable rule id + located subject):

* a **rule** is a *source pattern* (``nursery:dead-worker``,
  ``review:empty:{reviewer}``) plus a *subject pattern* over the
  fingerprint (``dead-worker:{host}:{process}``; ``""`` for a singleton
  whose fingerprint is fixed); ``{name}`` marks a per-instance variable.
  The spelling ``<source>/<fingerprint>`` is the addressable id —
  sources never contain ``/`` (enforced in :func:`precis.alerts.raise_alert`),
  fingerprints may (``pass-dead:{host}/{process}/{handler}``,
  ``caspar:/``), so ``id.split("/", 1)`` round-trips.
* :func:`resolve` maps a concrete ``(source, fingerprint)`` to its rule
  by pattern match; ``raise_alert`` stamps the result as ``meta.rule_id``
  on every row it writes, so a raised alert carries its rule.
* ``service`` points at the owning :class:`~precis.workers.registry.ServiceSpec`
  by name — the agent read renders that spec's ``one_line`` /
  ``doc_skill`` / ``log_handler`` instead of this module carrying a
  second runbook pointer that could rot. ``triage`` is the one line
  "first thing to check"; it is required for every ``critical`` rule.
* ``budget`` is the window the condition is judged against (``"2h"``,
  ``"7d"``, ``"interval+margin"``), free text, because the producers'
  budgets are not all hours.

Modelled on ``workers/registry.py`` (frozen slotted dataclass, a module
tuple plus a by-id dict). ``tests/test_alert_ids.py`` is the totality
gate: every ``raise_alert`` call site in ``src/`` must resolve to a
registered source pattern and every registered source must have a
raiser, and every fixed-name ``health_digest`` check must be pinned to a
registered subject — so a rename is a reviewed id change, never a silent
new failure plus a silent auto-resolve of the old one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

_VAR_RE = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True, slots=True)
class AlertRule:
    """One registered failure id (see module docstring for the fields)."""

    rule: str
    subject: str
    one_line: str
    severity: str
    service: str | None
    triage: str = ""
    budget: str = ""

    @property
    def id(self) -> str:
        """The addressable ``<source>/<fingerprint>`` pattern."""
        return f"{self.rule}/{self.subject}" if self.subject else self.rule

    @property
    def is_singleton(self) -> bool:
        """``True`` when the id has no ``{var}`` — one concrete instance."""
        return not _VAR_RE.search(self.id)


def _watchdog(
    group: str,
    subject: str,
    one_line: str,
    *,
    budget: str,
    service: str | None,
    severity: str = "warn",
    triage: str = "",
) -> AlertRule:
    return AlertRule(
        f"watchdog:{group}", subject, one_line, severity, service, triage, budget
    )


def _nursery(
    category: str,
    subject: str,
    one_line: str,
    *,
    severity: str,
    service: str | None = "nursery",
    budget: str = "",
    triage: str = "",
) -> AlertRule:
    return AlertRule(
        f"nursery:{category}", subject, one_line, severity, service, triage, budget
    )


ALERT_RULES: tuple[AlertRule, ...] = (
    # ── health_digest: curated Layer-1 outcome checks ──────────────────
    _watchdog(
        "ingest",
        "papers_ingested",
        "No new paper ref landed within budget.",
        budget="6h",
        service="watch",
    ),
    _watchdog(
        "ingest",
        "news",
        "No new news ref landed within budget.",
        budget="8h",
        service="news_poll",
    ),
    _watchdog(
        "ingest",
        "chunks_extracted",
        "A paper newer than the newest chunk has gone unextracted past budget.",
        budget="6h",
        service="watch",
    ),
    _watchdog(
        "discovery",
        "chunks_classified",
        "No role3 chunk classification within budget while the classify pass "
        "is enabled and has eligible chunks.",
        budget="12h",
        service="classify",
    ),
    _watchdog(
        "discovery",
        "embed",
        "Embed backlog non-empty and not draining past budget (idle-aware: "
        "0 pending is healthy whatever the last batch age).",
        budget="2h",
        service="embedder",
        triage=(
            "Read the check's detail: it names the first stuck stage "
            "(materializer → embed_batch jobs → job_inproc). An absent "
            "embedder process is NOT evidence — the service idle-unloads."
        ),
    ),
    _watchdog(
        "discovery",
        "chunk_keywords",
        "chunk_keywords backlog non-empty and not draining past budget (idle-aware).",
        budget="6h",
        service="chunk_keywords",
    ),
    _watchdog(
        "reading",
        "morning_brief_cast",
        "The morning briefing pass has not run within budget.",
        budget="26h",
        service="briefing",
    ),
    _watchdog(
        "reading",
        "cast_audio",
        "cast_audio has not narrated a cast within budget.",
        budget="26h",
        service="cast_audio",
    ),
    _watchdog(
        "reading",
        "card_forge",
        "No card_forge job completed within budget while cards were due.",
        budget="26h",
        service=None,
    ),
    _watchdog(
        "knowledge",
        "taproot_edges",
        "Eligible chase findings outstanding past budget with no new taproot "
        "evidence edge.",
        budget="6h",
        service="chase",
    ),
    _watchdog(
        "taproot",
        "claim_hub_dedup_index",
        "A live claim hub is missing the finding_body embedding claim dedup "
        "retrieves over.",
        budget="eval",
        service="hub_refine",
    ),
    _watchdog(
        "nanopub",
        "staged_candidates_fresh",
        "Staged nanopub candidates no longer pass the current mint gates.",
        budget="eval",
        service=None,
    ),
    _watchdog(
        "autonomy",
        "agent_jobs_completing",
        "Agent jobs were claimed in the window but none completed.",
        budget="6h",
        service="job_claude_inproc",
    ),
    _watchdog(
        "autonomy",
        "doctor_report_fresh",
        "The doctor tick has not written a report within twice its fresh window.",
        budget="2×FRESH_WINDOW",
        service=None,
    ),
    _watchdog(
        "infra",
        "hosts_alive",
        "A host heartbeat row has gone silent past the silence budget.",
        budget="15min",
        service="heartbeat",
    ),
    _watchdog(
        "meta",
        "alert_backlog_rot",
        "Open alerts older than 7 days — the response loop itself is rotting.",
        budget="7d",
        service="health_digest",
        severity="info",
    ),
    # ── health_digest: derived checks ──────────────────────────────────
    _watchdog(
        "cadence",
        "{cadence}",
        "A scheduler_leases cadence is overdue past interval+margin.",
        budget="interval+margin (≥15min)",
        service="scheduler",
    ),
    _watchdog(
        "cadence",
        "{cadence}/never-seeded",
        "A registered cadence has no scheduler_leases row on any host — its "
        "eligibility gate may be false fleet-wide.",
        budget="eval",
        service="scheduler",
    ),
    _watchdog(
        "coherence",
        "{service}",
        "A registered PASS resolves enabled somewhere but logged nothing in "
        "24h — intended-on but silent.",
        budget="24h",
        service="health_digest",
    ),
    # ── health_digest: condition registry (workers/conditions.py) ──────
    _watchdog(
        "condition",
        "pass-dead:{host}/{process}/{handler}",
        "A pass that logged regularly on a live host has gone silent past its budget.",
        budget="PRECIS_PASS_DEAD_BUDGET_S",
        service="health_digest",
    ),
    _watchdog(
        "condition",
        "rescue-gap:{handler}",
        "A rescue handler has not run within budget while a live host has demand.",
        budget="per handler",
        service="health_digest",
    ),
    _watchdog(
        "condition",
        "pass-wedged:{host}/{process}",
        "A live host's pass stopped completing cycles past the wedged budget.",
        budget="per probe",
        service="health_digest",
    ),
    _watchdog(
        "condition",
        "llm-degraded:{model}/{transport}/{tier}",
        "An LLM route's recent failure ratio is over the degraded threshold.",
        budget="per probe",
        service="llm_reconcile",
    ),
    _watchdog(
        "condition",
        "agent-ticks-toolless",
        "Agent ticks are spending without calling a single precis tool.",
        budget="per probe",
        service="job_claude_inproc",
    ),
    _watchdog(
        "condition",
        "dead-gen-claims:{host}/{process}",
        "Claims held by a worker generation that is no longer alive, past the age budget.",
        budget="per probe",
        service="health_digest",
    ),
    _watchdog(
        "condition",
        "settings-env-shadowed:{host}/{key}",
        "A host still sets an env var for a key that now resolves from app_settings (visibility, not a fault).",
        budget="per probe",
        service="health_digest",
        severity="info",
    ),
    _watchdog(
        "condition",
        "analysis-stale:{slug}/fi{ref_id}",
        "An analyzed-by link pins a design sha that cad_save has since moved.",
        budget="per probe",
        service="health_digest",
    ),
    # ── nursery detectors (workers/nursery.py) ─────────────────────────
    _nursery(
        "spin-loop",
        "spin-loop:{ref_id}",
        "One (ref, source) produced > 200 ref_events in 24h.",
        severity="warn",
    ),
    _nursery(
        "plan-tick-spin",
        "plan-tick-spin:{ref_id}",
        "A planner parent re-minted many plan_tick jobs in 24h.",
        severity="warn",
    ),
    _nursery(
        "quest-loop-failing",
        "quest-loop-failing:{ref_id}",
        "A quest's quest_tick loop keeps resting STATUS:failed.",
        severity="warn",
    ),
    _nursery(
        "orphaned-coordinator",
        "orphaned-coordinator:{ref_id}",
        "An active coordinator whose newest loop failed and nothing re-minted.",
        severity="critical",
        triage="get(kind='todo', id=<ref_id>) — close or re-dispatch it.",
    ),
    _nursery(
        "stale-claim",
        "stale-claim:{ref_id}",
        "A claimed-by:* tag older than 3h.",
        severity="warn",
    ),
    _nursery(
        "long-wait",
        "long-wait:{ref_id}",
        "A waiting-for:* tag older than 7d.",
        severity="info",
    ),
    _nursery(
        "stuck-doable",
        "stuck-doable:{ref_id}",
        "A dispatch-candidate leaf idle > 24h.",
        severity="info",
    ),
    _nursery(
        "child-failed-parked",
        "child-failed-parked:{ref_id}",
        "A todo parked on an open child-failed:<job> tag past the budget.",
        severity="warn",
    ),
    _nursery(
        "child-failed-final",
        "child-failed-final:aggregate",
        "Final-failed children, aggregated into one alert.",
        severity="warn",
    ),
    _nursery(
        "stalled-recurring",
        "stalled-recurring:{ref_id}",
        "A recurring todo whose last child is stuck.",
        severity="warn",
    ),
    _nursery(
        "worker-restart",
        "worker-restart:{host}:{process}",
        "A worker process restarted repeatedly in the window (restart storm).",
        severity="critical",
        triage=(
            "get(kind='job', id='/logs?host=<host>&process=<process>&level="
            "ERROR&since=2') — the crash reason is the first ERROR after a boot."
        ),
    ),
    _nursery(
        "dead-worker",
        "dead-worker:{host}:{process}",
        "A worker process stopped heartbeating.",
        severity="critical",
        triage=(
            "Check the host heartbeat first (host-dark?), then that process's "
            "worker_logs; a daemon that idle-unloads (embedder) is NOT a worker."
        ),
    ),
    _nursery(
        "nas-denied",
        "nas-denied:{host}",
        "A host's workers cannot read the NAS corpus mount (per host, or per "
        "host:process when attributable).",
        severity="critical",
        triage="Remount the corpus share on <host>; see precis-nursery-help.",
    ),
    _nursery(
        "nas-denied",
        "nas-denied:{host}:{process}",
        "One worker process on a host is denied the NAS corpus mount.",
        severity="critical",
        triage="Remount the corpus share on <host>; see precis-nursery-help.",
    ),
    _nursery(
        "host-dark",
        "host-dark:{host}",
        "A fleet host's heartbeat has gone dark.",
        severity="critical",
        triage=(
            "Ping/ssh the host; if it is up, the heartbeat pass or its worker "
            "unit is down — read get(kind='job', id='/logs?host=<host>')."
        ),
    ),
    _nursery(
        "dispatch-stall",
        "dispatch-stall",
        "The single agent-profile executor stopped claiming — planner dark.",
        severity="critical",
        service="minter",
        triage="Is the dispatch pass alive and are executor slots advertised?",
    ),
    _nursery(
        "embed-lane-stalled",
        "embed-lane-stalled",
        "embed_batch jobs are queued but none has completed in the window.",
        severity="critical",
        service="job_inproc",
        triage=(
            "Read watchdog:discovery/embed's detail for the stuck stage; "
            "process presence is not the test (the embedder idle-unloads)."
        ),
    ),
    _nursery(
        "lane-skipping",
        "lane-skipping:{job_type}",
        "A job_type whose runs are mostly cancelled — down while looking clean.",
        severity="warn",
    ),
    # ── other producers ────────────────────────────────────────────────
    AlertRule(
        "nanopub-demote",
        "contradicted-frozen:{hub_ref_id}",
        "A frozen (published) claim hub acquired a live contradicts edge.",
        "warn",
        None,
    ),
    AlertRule(
        "nanopub_mirror",
        "concurrence:{artifact_code}:fi{claim_ref_id}",
        "A mirrored nanopub artifact concurs with a local claim.",
        "info",
        None,
    ),
    AlertRule(
        "nanopub_ots",
        "stuck-pending:{batch_id}",
        "An OpenTimestamps batch has stayed pending past budget.",
        "warn",
        None,
    ),
    AlertRule(
        "nanopub_ots",
        "audit-mismatch",
        "The OTS audit found a published artifact whose proof no longer verifies.",
        "critical",
        None,
        triage="Re-run the OTS audit; a persistent mismatch means a tampered "
        "or re-generated artifact — do not re-stamp until explained.",
    ),
    AlertRule(
        "admit:oversize",
        "{model}:{source}",
        "A prompt exceeded the model's admitted context size at one call site.",
        "warn",
        None,
    ),
    AlertRule(
        "watch:elsevier_truncation",
        "elsevier-truncation:{ref_id}",
        "An Elsevier PDF ingested truncated (publisher-side cut-off).",
        "critical",
        "watch",
        triage="Re-fetch via the OA path; see the alert detail for the ref.",
    ),
    AlertRule(
        "ingest:marker-fallback",
        "marker-fallback",
        "PDF ingest fell back from marker to the plain extractor.",
        "warn",
        "watch",
    ),
    AlertRule(
        "inject_scan",
        "{account}:{folder}:{uidvalidity}:{uid}",
        "A scanned mail message carried a prompt-injection marker.",
        "warn",
        "inject_scan",
    ),
    AlertRule(
        "llm_reconcile:drift",
        "proxy-missing:{model_id}",
        "A catalogued model is missing from the proxy's served list.",
        "warn",
        "llm_reconcile",
    ),
    AlertRule(
        "quota_check:auth",
        "{host}:claude-oauth",
        "The Claude OAuth credential on a host is expired or unreadable.",
        "critical",
        "quota_check",
        triage="Re-login on <host> (`claude login`); agent ticks there are "
        "failing until then.",
    ),
    AlertRule(
        "review:empty:{reviewer}",
        "{reviewer}:empty-pass",
        "An LLM reviewer pass produced no output and no tool calls ($0).",
        "warn",
        None,
    ),
    AlertRule(
        "review:tool-starved:{reviewer}",
        "{reviewer}:tool-starved",
        "An LLM reviewer pass wrote prose but never called a precis tool.",
        "warn",
        None,
    ),
    AlertRule(
        "disk_check",
        "{host}:{path}",
        "Free disk on a watched path is under threshold (critical at the hard "
        "line, warn at the soft one).",
        "critical",
        "disk_check",
        triage="`df -h <path>` on <host>; the usual eaters are docker images "
        "and /var/log — see precis-nursery-help.",
        budget="per pass",
    ),
    AlertRule(
        "fetch_oa:openalex_balance",
        "openalex-content-credits-low",
        "The OpenAlex content-credit balance is below the low-water mark.",
        "warn",
        "fetch",
    ),
    AlertRule(
        "orcid_enrich",
        "orcid_enrich:missing_credentials",
        "ORCID enrichment has no API credentials configured.",
        "warn",
        "orcid_enrich",
    ),
    AlertRule(
        "scheduler",
        "unschedulable:{ref_id}",
        "A todo cannot be scheduled by any executor profile.",
        "warn",
        "sweeper",
    ),
    AlertRule(
        "quest_tick",
        "quest:dry-rest/{quest_id}",
        "A quest tick is resting with nothing runnable (dry rest).",
        "warn",
        "quest_loop_reconcile",
    ),
    AlertRule(
        "budget:quota",
        "quota-{window}",
        "An LLM quota window is exhausted — the breaker is open.",
        "critical",
        None,
        triage="get(kind='llm', id='/budget'); nothing LLM-backed runs until the "
        "window rolls or the cap is raised.",
        budget="per window",
    ),
    AlertRule(
        "budget",
        "cap-{window}",
        "An LLM spend cap for a window is hit — the breaker is open.",
        "critical",
        None,
        triage="get(kind='llm', id='/budget'); raise the cap deliberately or wait "
        "for the window.",
        budget="per window",
    ),
)

RULES_BY_ID: dict[str, AlertRule] = {r.id: r for r in ALERT_RULES}
assert len(RULES_BY_ID) == len(ALERT_RULES), "duplicate alert rule id"


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile an id pattern: literal text escaped, every ``{var}`` a
    greedy ``.+`` (a variable may itself contain ``/`` or ``:`` —
    ``llm-degraded:z-ai/glm-4.7-flash/openai_compat/cloud``)."""
    return _pattern_regex(pattern)


@lru_cache(maxsize=512)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = _VAR_RE.split(pattern)
    return re.compile(".+".join(re.escape(p) for p in parts))


def _literal_len(pattern: str) -> int:
    return len(_VAR_RE.sub("", pattern))


def matches_source(rule: AlertRule, source: str) -> bool:
    return _pattern_regex(rule.rule).fullmatch(source) is not None


def resolve(source: str, fingerprint: str) -> AlertRule | None:
    """The rule a concrete ``(source, fingerprint)`` instance belongs to,
    or ``None`` when unregistered. When several patterns match (``nas-
    denied:{host}`` vs ``nas-denied:{host}:{process}``) the one with the
    most literal text wins — the more specific spelling."""
    best: AlertRule | None = None
    best_len = -1
    for rule in ALERT_RULES:
        if not matches_source(rule, source):
            continue
        subj = rule.subject
        ok = (
            _pattern_regex(subj).fullmatch(fingerprint) is not None
            if subj
            else fingerprint == ""
        )
        if not ok:
            continue
        lit = _literal_len(rule.id)
        if lit > best_len:
            best, best_len = rule, lit
    return best


def rule_id(source: str, fingerprint: str) -> str | None:
    """:func:`resolve` → the registered id string, or ``None``."""
    rule = resolve(source, fingerprint)
    return rule.id if rule is not None else None


def split_id(id_str: str) -> tuple[str, str]:
    """``<source>/<fingerprint>`` → the pair, splitting on the FIRST ``/``
    only (fingerprints may contain ``/``; sources never do)."""
    source, _, fingerprint = id_str.partition("/")
    return source, fingerprint


__all__ = [
    "ALERT_RULES",
    "RULES_BY_ID",
    "AlertRule",
    "matches_source",
    "pattern_regex",
    "resolve",
    "rule_id",
    "split_id",
]
