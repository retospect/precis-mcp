"""run_finding_chase_pass — sibling worker that advances finding chains.

Ref-level workers are sibling functions, not
``WorkerHandler`` subclasses. This module follows the same shape as
``precis.workers.segment_toc``:

- ``claim_tracing_findings`` — derived-queue claim over ``refs`` +
  ``ref_tags`` for ``STATUS:tracing`` findings.
- ``advance_finding`` — one chase hop per call (frontier → next ref
  + ``derived-from`` link + ``meta.chain`` append, or terminal
  decision + chain-snapshot pass).
- ``run_finding_chase_pass`` — runner-side entry point; returns
  observability tuple ``{claimed, ok, failed}``.

The worker is **deterministic by default** (regex + S2 + chain
membership). With ``with_llm=True`` (or env ``PRECIS_CHASE_LLM=1``)
three :mod:`precis.utils.claude_p` hooks light up:

- ``_disambiguate_candidates`` resolves multi-cite chunks.
- ``_locate_chunk_in_target`` confirms the ANN's chunk pick.
- ``_verify_support_with_caveats`` reads the target chunk + claim
  and records support / caveats / cited-others on the chain entry.

Path B-ii: the chase walks ``links`` + ``chunks`` directly. It
does **not** create ``kind='citation'`` records (those stay
strictly user / verifier-subagent authored). Auto-spawning
sibling findings for caveat-referenced cites is also out — the
user spawns them by hand when a qualification matters.

Cost: ``--with-llm`` costs ~$0.05–$0.10 per established finding
(3 hops × ~$0.01 verifier calls under Haiku). Deterministic
default costs zero.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.taproot.canon import (
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    CanonicalClaim,
    Placement,
    block,
    dedup_judge,
    place,
)
from precis.taproot.hub import apply_placement, attach_evidence
from precis.workers._chase_llm import (
    _disambiguate_candidates,
    _locate_chunk_in_target,
    _verify_support_with_caveats,
)

log = logging.getLogger(__name__)

# Source slug for every event the chase writes to ref_events. Readers
# (`get(kind='finding', view='log')`, cross-ref incident queries) filter
# on this so chase activity is separable from segments / fetcher / etc.
_SOURCE = "chase"

# Taproot Phase-3 W1 forward bridge (mint/attach a claim hub off an
# established finding's terminal verdict). Default-OFF, INDEPENDENT of
# PRECIS_CHASE_LLM: the bridge only fires when this is on *and* the chase
# LLM verdict is available (with_llm=True produced a `verification`), so
# the deterministic chase path is unaffected either way.
_TAPROOT_CHASE_ENV = "PRECIS_TAPROOT_CHASE_ENABLED"


# ── Constants ──────────────────────────────────────────────────────

_STATUS_NAMESPACE = "STATUS"
_TRACING = "tracing"
_ACQUIRING = "acquiring"
_ESTABLISHED = "established"
_DEAD_CHAIN = "dead_chain"
_CYCLE = "cycle"
_MULTI_CANDIDATE = "multi_candidate"

_DERIVED_FROM = "derived-from"
_AWAITS_EVIDENCE = "awaits-evidence"

# The axis:taproot classifier's idempotency-marker namespace
# (axis_pass.py mints it as f"{axis_id.upper()}CASCADE"). Its PRESENCE
# alongside TAPROOT:claim is what distinguishes a classifier-labeled
# *live* finding from a real mint_hub claim hub: mint_hub writes the
# claim tag but NEVER this marker (see the claim-query exclusion below).
_TAPROOT_CASCADE_NS = f"{TAPROOT_NAMESPACE}CASCADE"

# Acquisition-mode give-up reason: a
# dead_chain transition written when every linked awaits-evidence stub is
# fetch-exhausted (see :func:`_stub_exhausted`) past the grace window.
_UNACQUIRABLE = "unacquirable"

# Env override for the acquisition-mode grace window — how long an
# `acquiring` finding may sit on fetch-exhausted stubs before the chase
# gives up on it (flips to `dead_chain(reason=unacquirable)`). Decided
# 2026-08-04 (post-review): env-tunable, default 7 days. Distinct from
# WAITING_ABANDON_AFTER_DAYS below (14d) — that one fires on elapsed time
# alone for the *tracing* arm's frontier-stub wait; this one additionally
# requires every linked stub to be fetch-exhausted (see the module docs
# for the "honest give-up" rationale: age alone isn't enough when the
# stub might still land a PDF).
_ACQUIRE_GRACE_ENV = "PRECIS_ACQUIRE_GRACE_DAYS"
ACQUIRE_GRACE_DAYS_DEFAULT = 7.0

# Backoff for findings stuck on a chunk-less frontier stub. When
# ``advance_finding`` returns ``"waiting"`` it leaves STATUS:tracing
# unchanged, so without a backoff the claim re-picks the same finding
# every pass (per minute, per cluster node) and floods ref_events with
# identical ``waiting`` rows — observed at >1000/day on a handful of
# refs. We skip a finding whose most-recent chase event is a
# ``waiting`` newer than the effective window; a finding that last
# *advanced* (or any non-waiting outcome) is never suppressed, so real
# progress stays prompt.
#
# The window is *exponential*, mirroring the OA fetcher
# (``fetch_oa.claim_stubs_to_fetch``): the effective wait doubles per
# consecutive ``waiting`` outcome — ``base * 2^(waits-1)`` capped at
# :data:`WAITING_BACKOFF_MAX_MINUTES`. A flat window re-polls a
# never-arriving stub once an hour *forever* (24/day/ref); the
# frontier stub's own PDF fetch backs off to monthly, so chase
# re-poking it hourly is pure waste. With the exponential window a
# finding that keeps waiting settles to ~one poll/day instead. The run
# resets to ``base`` the moment the finding makes any progress.
WAITING_BACKOFF_MINUTES = 60

# Cap on the exponential waiting window. 1440 min = 24h: after ~5
# consecutive waits (60→120→240→480→960→capped) a stuck finding polls
# at most once a day, which still picks up a late-arriving stub PDF
# within a day while killing the per-minute flood.
WAITING_BACKOFF_MAX_MINUTES = 1440

# Terminal give-up. A one-a-day re-poke is cheap insurance for a stub
# whose PDF is merely late — but some frontier stubs *never* get a PDF
# (no OA version, withdrawn, paywalled forever), and re-poking those
# once a day in perpetuity is pure noise that keeps the finding in the
# tracing pool and the ``(ref_id, 'chase')`` pair ticking forever. Once
# a finding has been *continuously* waiting (no intervening progress)
# for this many days we abandon it: flip STATUS:tracing → dead_chain so
# it leaves the claim pool entirely. The threshold is measured in
# wall-clock age of the consecutive-waiting run, NOT a waits count, so
# a pre-fix spin-loop burst (hundreds of `waiting` rows in an hour)
# can't trip it early — only genuine multi-week starvation does.
WAITING_ABANDON_AFTER_DAYS = 14.0

# Inline citation patterns. Numbered bracket form is the most common
# and the cheapest to map (positional into S2's references list).
_NUMBERED_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# Author-year form — Miller et al. 2020 / (Miller 2020) / etc.
_AUTHOR_YEAR_RE = re.compile(
    r"""
    \(?
    ([A-Z][a-zA-Z']{1,30}(?:\s+et\s+al\.?)?(?:\s+and\s+[A-Z][a-zA-Z']{1,30})?)
    [,\s]+
    (\d{4})[a-z]?
    \)?
    """,
    re.VERBOSE,
)


# ── Result types ───────────────────────────────────────────────────


@dataclass(frozen=True)
class FindingRow:
    """The minimal claim-batch shape per finding."""

    ref_id: int
    title: str
    meta: dict[str, Any]
    #: STATUS tag value at claim time — ``'tracing'`` or ``'acquiring'``
    #: (the acquisition-mode mint). Decides which arm of
    #: :func:`advance_finding` runs. Defaults to ``'tracing'`` when no
    #: STATUS tag is found (defensive; shouldn't happen for a row this
    #: query claims).
    status: str = _TRACING


@dataclass
class PassResult:
    """Counts per chase pass for observability."""

    claimed: int = 0
    advanced: int = 0  # made a hop
    terminated: int = 0  # established a chain
    dead: int = 0  # tagged STATUS:dead_chain
    multi: int = 0  # tagged STATUS:multi_candidate
    cycled: int = 0  # tagged STATUS:cycle
    waiting: int = 0  # frontier stub still has no chunks (no-op pass)
    failed: int = 0  # exception escaped advance_finding


@dataclass
class _Event:
    """Per-pass event the chase builds up and writes to ref_events.

    Mutated as ``advance_finding`` learns things. The runner flushes
    one row per pass via ``store.append_event`` with these fields
    spread across the dedicated columns (``ts``, ``duration_ms``,
    ``cost_usd``) and the rest in ``payload``.
    """

    decision: str = ""  # set at the return point
    frontier: dict[str, Any] = field(default_factory=dict)
    next: dict[str, Any] | None = None  # set on hop / multi
    reason: str | None = None  # set on dead
    inline_cites_detected: list[str] = field(default_factory=list)
    llm: dict[str, Any] | None = None  # {"hook": ..., "cost_usd": ..., "model": ...}
    cost_usd: float | None = None
    error: str | None = None  # set on failed


# ── Claim query ────────────────────────────────────────────────────


def claim_tracing_findings(
    conn: Connection,
    *,
    limit: int,
    waiting_backoff_minutes: int = WAITING_BACKOFF_MINUTES,
    waiting_backoff_max_minutes: int = WAITING_BACKOFF_MAX_MINUTES,
) -> list[FindingRow]:
    """Lock and return up to ``limit`` ``STATUS:tracing`` OR
    ``STATUS:acquiring`` findings.

    Widened for acquisition-mode findings: this is the *sole* feeder
    of :func:`advance_finding`, so an
    ``acquiring`` row that isn't claimed here never reaches its arm —
    the claim query is the load-bearing half of making that arm
    reachable in the real worker loop, not just in a hand-built unit
    test. Each returned :class:`FindingRow` carries its own ``status``
    so :func:`advance_finding` dispatches to the right arm.

    ``FOR UPDATE OF r SKIP LOCKED`` lets concurrent chase workers
    coexist — each one claims a disjoint subset. The lock is held
    for the lifetime of the *outer* transaction; the caller is
    responsible for committing per-finding so the lock window stays
    short.

    Findings whose most-recent chase event is a ``waiting`` newer than
    the *effective* backoff window are skipped: their frontier stub
    still has no chunks, so re-walking them every pass is a pure no-op
    that only churns ref_events. The window is **exponential** — it
    doubles per consecutive ``waiting`` outcome,
    ``waiting_backoff_minutes * 2^(waits-1)`` capped at
    ``waiting_backoff_max_minutes`` (see :data:`WAITING_BACKOFF_MINUTES`
    / :data:`WAITING_BACKOFF_MAX_MINUTES`). The ``waits`` count is the
    run of ``waiting`` events since the finding's last non-waiting
    outcome, so any progress resets the backoff to ``base``. Any other
    most-recent outcome (or none yet) leaves the finding eligible, so a
    chain that just advanced keeps moving promptly. This same
    per-``ref_id`` backoff — keyed on ``source='chase'`` regardless of
    *why* a finding is waiting — also throttles an ``acquiring`` finding
    whose linked stub(s) have no chunks yet (:func:`_advance_acquiring`
    returns the same ``"waiting"`` outcome), and its accumulated age
    feeds the acquisition-mode grace-window give-up check.

    Excludes a *real* ``mint_hub`` claim hub — ``TAPROOT:claim`` with NO
    ``TAPROOTCASCADE`` marker. A hub mints ``STATUS:canonical`` (already
    off this ``STATUS:tracing`` filter), but the tag exclusion stays as a
    belt-and-suspenders guard for a *mis-statused* hub: one that somehow
    carried ``STATUS:tracing`` would otherwise re-claim + die as an
    empty-chain ``dead_chain`` every pass (gripe 175806). Crucially it
    excludes ONLY the marker-less (mint_hub) claim tag: the
    ``axis:taproot`` classifier stamps ``TAPROOT:claim`` + a
    ``TAPROOTCASCADE`` marker onto a *live* tracing finding that owns a
    real chase chain, and those must stay claimable or they freeze —
    neither chased nor canonical (OPEN-ITEMS §axis:taproot
    promote-and-freeze). Real hubs are chased by the taproot seniority
    derivation, not this worker.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, r.meta,
               COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                   WHERE rt.ref_id = r.ref_id AND t.namespace = %(status_ns)s
                   LIMIT 1),
                 %(tracing)s
               ) AS status
          FROM refs r
          LEFT JOIN LATERAL (
                SELECT e.event, e.ts FROM ref_events e
                 WHERE e.ref_id = r.ref_id AND e.source = %(source)s
                 ORDER BY e.ts DESC
                 LIMIT 1
          ) last_chase ON TRUE
          LEFT JOIN LATERAL (
                -- Run of consecutive ``waiting`` events since the last
                -- non-waiting chase outcome — the backoff "attempt"
                -- count. Resets to 0 the moment the finding advances
                -- (or hits any terminal/other outcome), so a chain that
                -- starts moving again is not penalised by old waits.
                SELECT count(*)::int AS waits FROM ref_events e
                 WHERE e.ref_id = r.ref_id AND e.source = %(source)s
                   AND e.event = 'waiting'
                   AND e.ts > COALESCE(
                         (SELECT max(e2.ts) FROM ref_events e2
                           WHERE e2.ref_id = r.ref_id
                             AND e2.source = %(source)s
                             AND e2.event <> 'waiting'),
                         '-infinity'::timestamptz
                       )
          ) wait_run ON TRUE
         WHERE r.kind = 'finding'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(status_ns)s
                    AND t.value = ANY(%(statuses)s)
               )
           -- Skip a REAL mint_hub claim hub (empty chain, not a chase-
           -- owned chain) so it can't re-claim + die as an empty-chain
           -- dead_chain every pass (gripe 175806). A real hub carries
           -- TAPROOT:claim written by mint_hub, which never writes a
           -- TAPROOTCASCADE marker. The axis:taproot CLASSIFIER, by
           -- contrast, writes TAPROOT:claim + TAPROOTCASCADE *together*
           -- onto a live STATUS:tracing finding that DOES own a real
           -- chase chain — excluding those froze the Malthus-draft
           -- claims out of the lifecycle (not chased, never canonical;
           -- OPEN-ITEMS §axis:taproot promote-and-freeze). So exclude
           -- only TAPROOT:claim that has NO cascade marker (= a mint_hub
           -- hub); a claim-tag carrying the marker stays claimable.
           AND NOT (
                 EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = %(taproot_ns)s
                      AND t.value = %(taproot_claim)s
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = %(cascade_ns)s
                 )
               )
           -- Skip only findings whose *most recent* chase outcome is a
           -- ``waiting`` still inside the exponential window. COALESCE
           -- makes the predicate NULL-safe: a finding with no chase
           -- events yet (the common case) has last_chase.* = NULL, the
           -- inner expression is NULL, and without the COALESCE
           -- ``NOT NULL`` would drop the row from the claim entirely.
           AND NOT COALESCE(
                 last_chase.event = 'waiting'
                 AND last_chase.ts > now() - (
                       LEAST(
                         %(base)s::double precision
                           -- Clamp the exponent before POWER: a finding
                           -- stuck in ``waiting`` for >~1024 cycles makes
                           -- ``2^(waits-1)`` overflow double precision and
                           -- Postgres raises ``value out of range``,
                           -- crashing the whole chase pass every loop. The
                           -- window is already pinned at ``cap`` once
                           -- ``base * 2^n >= cap`` (n≈5 for the defaults),
                           -- so clamping the exponent at 60 is
                           -- behaviour-preserving and overflow-proof.
                           * POWER(2, LEAST(GREATEST(wait_run.waits - 1, 0), 60)),
                         %(cap)s::double precision
                       ) * INTERVAL '1 minute'
                     ),
                 FALSE
               )
         ORDER BY r.ref_id
         LIMIT %(limit)s
           FOR UPDATE OF r SKIP LOCKED
        """,
        {
            "source": _SOURCE,
            "status_ns": _STATUS_NAMESPACE,
            "tracing": _TRACING,
            "statuses": [_TRACING, _ACQUIRING],
            "taproot_ns": TAPROOT_NAMESPACE,
            "taproot_claim": TAPROOT_CLAIM,
            "cascade_ns": _TAPROOT_CASCADE_NS,
            "base": float(waiting_backoff_minutes),
            "cap": float(waiting_backoff_max_minutes),
            "limit": limit,
        },
    ).fetchall()
    return [
        FindingRow(
            ref_id=int(r[0]), title=str(r[1]), meta=dict(r[2] or {}), status=str(r[3])
        )
        for r in rows
    ]


# ── Per-finding logic ──────────────────────────────────────────────


def advance_finding(
    conn: Connection,
    store: Any,
    finding: FindingRow,
    *,
    with_llm: bool = False,
    taproot_enabled: bool = False,
    taproot_embedder: Any = None,
) -> tuple[str, _Event]:
    """Advance one finding by at most one hop.

    Returns ``(outcome, event)`` — the outcome string (``"advanced"``
    / ``"terminated"`` / ``"dead"`` / ``"multi"`` / ``"cycle"`` /
    ``"waiting"``) plus the populated :class:`_Event` the runner
    flushes to ``ref_events``.

    Dispatches on ``finding.status`` first: an ``acquiring`` finding
    (the acquisition-mode mint) runs :func:`_advance_acquiring` —
    poll its linked ``awaits-evidence`` stubs, ground on the first one
    that gains chunks, or give up once every stub is fetch-exhausted
    past the grace window — never the ``tracing`` logic below (an empty
    ``meta.chain`` is expected and NOT ``dead_chain`` for this arm,
    unlike the tracing arm's own empty-chain check just below).

    ``taproot_enabled`` (Phase-3 W1 forward bridge) mints/attaches a
    taproot claim hub off the terminal verdict when a chain establishes
    -- see :func:`_taproot_bridge`. Only takes effect together with
    ``with_llm`` (the bridge needs the LLM ``verification`` verdict);
    ``taproot_embedder`` is the ``.embed_one``-shaped embedder
    :func:`precis.taproot.canon.block` needs — ``None`` degrades the
    bridge to a no-op (logged), never a crash. The SAME embedder is
    reused (also degrading to a lexical fallback on ``None``) by
    :func:`_advance_acquiring`'s claim-text grounding search — it's a
    general block-embedder, not taproot-specific, despite the name.
    """
    ev = _Event()
    if finding.status == _ACQUIRING:
        return _advance_acquiring(
            conn, store, finding, ev, with_llm=with_llm, embedder=taproot_embedder
        )
    chain = list(finding.meta.get("chain") or [])
    if not chain:
        _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="empty_chain")
        ev.reason = "empty_chain"
        return "dead", ev

    frontier = chain[-1]
    frontier_ref_id = int(frontier["ref_id"])
    frontier_ord = frontier.get("ord")
    ev.frontier = {"ref_id": frontier_ref_id, "ord": frontier_ord}

    # Resolve the frontier ref. Soft-deleted → dead chain.
    target = _fetch_ref(conn, frontier_ref_id)
    if target is None:
        _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="target_deleted")
        ev.reason = "target_deleted"
        return "dead", ev

    # Stub paper with no chunks yet → waiting (no-op pass), unless the
    # finding has been starving on this frontier long enough that we
    # give up (see :data:`WAITING_ABANDON_AFTER_DAYS`). Abandoning flips
    # STATUS:tracing → dead_chain so the finding leaves the tracing pool
    # instead of re-polling ~once a day forever.
    target_chunks = _fetch_chunks(conn, frontier_ref_id)
    if not target_chunks:
        waits, run_age_days = _waiting_run_stats(conn, finding.ref_id)
        if run_age_days >= WAITING_ABANDON_AFTER_DAYS:
            _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="abandoned_waiting")
            ev.reason = "abandoned_waiting"
            ev.frontier["waited_days"] = round(run_age_days, 1)
            ev.frontier["waits"] = waits
            return "dead", ev
        return "waiting", ev

    # Locate the relevant chunk in the target paper.
    target_chunk = _select_target_chunk(
        target_chunks, frontier_ord, finding, with_llm=with_llm
    )
    if target_chunk is None:
        _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="no_target_chunk")
        ev.reason = "no_target_chunk"
        return "dead", ev

    chunk_id, chunk_ord, chunk_text = target_chunk
    ev.frontier["chunk_id"] = chunk_id
    ev.frontier["resolved_ord"] = chunk_ord

    # Inline cite scan on the target chunk text.
    inline_cites = _detect_inline_cites(chunk_text)
    ev.inline_cites_detected = inline_cites

    verification = None
    if with_llm:
        verification = _verify_support_with_caveats(
            claim=_claim_body(conn, finding.ref_id),
            scope=finding.meta.get("scope") or {},
            target_cite_key=target.get("slug") or f"ref:{frontier_ref_id}",
            target_chunk_ord=chunk_ord,
            target_chunk_text=chunk_text,
        )
        chain[-1] = dict(chain[-1])
        chain[-1]["chunk_id"] = chunk_id
        chain[-1]["ord"] = chunk_ord
        if verification:
            chain[-1]["verification"] = verification
            ev.llm = {
                "hook": "verify",
                "supports": verification.get("supports"),
                "caveats_n": len(verification.get("caveats") or []),
                "cited_others_n": len(verification.get("cited_others") or []),
            }

    is_terminal = (
        not inline_cites if verification is None else bool(verification.get("terminal"))
    )

    if is_terminal:
        _snapshot_chain(conn, store, finding.ref_id, chain)
        if taproot_enabled and verification is not None:
            _taproot_bridge(
                conn,
                store,
                finding,
                chain=chain,
                verification=verification,
                target=target,
                chunk_ord=chunk_ord,
                embedder=taproot_embedder,
            )
        return "terminated", ev

    s2_refs_loaded = _load_s2_references(target.get("identifiers") or {})
    next_target = _pick_next_hop(
        inline_cites=inline_cites,
        s2_references=s2_refs_loaded,
        with_llm=with_llm,
        chunk_text=chunk_text,
    )

    if next_target is None:
        _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="no_resolvable_cite")
        ev.reason = "no_resolvable_cite"
        return "dead", ev
    if isinstance(next_target, _MultiCandidate):
        _set_status(conn, finding.ref_id, _MULTI_CANDIDATE)
        _record_candidates(conn, store, finding.ref_id, next_target.candidates)
        ev.next = {"candidates": len(next_target.candidates)}
        return "multi", ev

    next_ref_id = _resolve_or_create_stub(conn, store, next_target)
    if next_ref_id is None:
        _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="no_external_id")
        ev.reason = "no_external_id"
        return "dead", ev

    if any(int(h["ref_id"]) == next_ref_id for h in chain):
        _set_status(conn, finding.ref_id, _CYCLE)
        ev.next = {"ref_id": next_ref_id, "would_cycle": True}
        return "cycle", ev

    chain.append({"ref_id": next_ref_id, "chunk_id": None, "ord": None})
    store.add_link(
        src_ref_id=finding.ref_id,
        dst_ref_id=next_ref_id,
        dst_pos=None,
        relation=_DERIVED_FROM,
        conn=conn,
    )
    store.update_ref(finding.ref_id, meta_patch={"chain": chain}, conn=conn)
    ev.next = {"ref_id": next_ref_id}
    return "advanced", ev


# ── Acquisition-mode arm (acquiring-finding grounding) ────────────────


def _acquire_grace_days() -> float:
    """The acquisition-mode give-up grace window, in days.

    Reads :data:`_ACQUIRE_GRACE_ENV` (``PRECIS_ACQUIRE_GRACE_DAYS``);
    falls back to :data:`ACQUIRE_GRACE_DAYS_DEFAULT` (7) on unset or
    unparseable input. Read fresh per call (not cached) so a live env
    change (or a test's monkeypatch) takes effect on the next pass.
    """
    raw = os.environ.get(_ACQUIRE_GRACE_ENV)
    if not raw:
        return ACQUIRE_GRACE_DAYS_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return ACQUIRE_GRACE_DAYS_DEFAULT


def _stub_exhausted(conn: Connection, stub_ref_id: int) -> bool:
    """Fetch-exhaustion judgment for one ``awaits-evidence`` stub.

    True when the stub has **no fetchable identifier at all** (doi /
    arxiv / s2 — the same trio :mod:`precis.store._stub_predicate` uses;
    a bare title+url stub never even enters ``fetch_oa``'s claim query,
    so it's exhausted from the moment it's minted), OR ``fetch_oa`` has
    already run **at least one** cascade attempt against it that came up
    empty (any ``source LIKE 'fetcher:%'`` ref_event whose ``event`` is
    NOT ``fetch_ok``) and it still carries no PDF.

    **A ``fetch_ok`` event always means NOT exhausted**, checked first
    and unconditionally — regardless of grace-window age or any earlier
    failed leg. ``fetch_ok`` means a leg downloaded the PDF into the
    watch inbox; ingest (``precis watch`` → ``precis_add`` →
    ``register_aliases_and_maybe_upgrade``) runs as a LATER, separate
    pass that sets ``refs.pdf_sha256`` and writes the chunks this
    function's caller is polling for. Without this check, a stub whose
    PDF landed but hasn't been ingested yet reads as indistinguishable
    from a stub every leg failed on — and if the grace window had
    already elapsed (e.g. a sibling ``wants=`` stub failed earlier), the
    acquiring arm would fire ``dead_chain(unacquirable)`` the instant
    before evidence actually arrives: silent, permanent loss right as
    the claim was about to be grounded. Picked "exclude fetch_ok from
    tried" over a ``refs.pdf_sha256 IS NULL`` co-condition because
    ``pdf_sha256`` isn't set until ingest completes — the same later
    step that lands chunks — so it can't distinguish
    "PDF fetched, awaiting ingest" from "never fetched" either; the
    ``fetch_ok`` event is the fetch cascade's own, immediate signal.

    This is deliberately NOT the same thing as "``fetch_oa`` has given
    up" — it never does; a closed-access stub just backs off to a
    monthly retry forever (see ``fetch_oa.claim_stubs_to_fetch``), since
    a paper can become OA later. This is a SEPARATE, acquiring-arm-only
    judgment about whether continuing to poll the *finding* is still
    honest hope, feeding the grace-window give-up in
    :func:`_advance_acquiring` — the stub itself is untouched either way
    and stays in the hand-download queue (``f971f012``).
    """
    row = conn.execute(
        """
        SELECT
            EXISTS (SELECT 1 FROM ref_identifiers
                     WHERE ref_id = %(rid)s
                       AND id_kind IN ('doi', 'arxiv', 's2')) AS has_id,
            EXISTS (SELECT 1 FROM ref_events
                     WHERE ref_id = %(rid)s
                       AND source LIKE 'fetcher:%%'
                       AND event <> 'fetch_ok') AS tried_and_failed,
            EXISTS (SELECT 1 FROM ref_events
                     WHERE ref_id = %(rid)s
                       AND source LIKE 'fetcher:%%'
                       AND event = 'fetch_ok') AS fetch_succeeded
        """,
        {"rid": stub_ref_id},
    ).fetchone()
    if row is None:
        return True
    has_id, tried_and_failed, fetch_succeeded = (
        bool(row[0]),
        bool(row[1]),
        bool(row[2]),
    )
    if fetch_succeeded:
        return False  # PDF fetched, ingest pending -- never exhausted
    return (not has_id) or tried_and_failed


def _select_grounding_chunk(
    store: Any,
    embedder: Any,
    claim_text: str,
    stub_ref_id: int,
    chunks: list[tuple[int, int, str]],
) -> tuple[int, int, str]:
    """Pick the chunk in a freshly-grounded stub that best supports the
    claim: ``(chunk_id, ord, text)``.

    Claim-text embedding search (:meth:`~precis.store.Store.search_blocks`
    ``mode='semantic'``, scoped to the stub) over the paper's own chunks
    when ``embedder`` is available — the "existing grounding machinery"
    the acquisition-mode design calls for. Degrades to the same
    lexical title/claim-overlap heuristic :func:`_select_target_chunk`
    uses for the ordinary chase when there's no embedder, or when the
    search errors — never a hard failure (mirrors the taproot forward
    bridge's own embedder-unavailable degrade).
    """
    if embedder is not None and claim_text.strip():
        try:
            query_vec = embedder.embed_one(claim_text)
            hits = store.search_blocks(
                q=claim_text,
                query_vec=query_vec,
                mode="semantic",
                kind="paper",
                scope_ref_id=stub_ref_id,
                limit=1,
            )
            if hits:
                block, _ref, _score = hits[0]
                match = next((c for c in chunks if c[0] == block.id), None)
                if match is not None:
                    return match
        except Exception:  # pragma: no cover — defensive
            log.warning(
                "chase: acquire-arm embedding search failed for stub "
                "ref_id=%s, falling back to lexical",
                stub_ref_id,
                exc_info=True,
            )
    claim_tokens = _tokenize(claim_text)
    if not claim_tokens:
        return chunks[0]
    return max(chunks, key=lambda c: _overlap(claim_tokens, _tokenize(c[2])))


def _ground_on_stub(
    conn: Connection,
    store: Any,
    finding: FindingRow,
    ev: _Event,
    *,
    stub_ref_id: int,
    chunks: list[tuple[int, int, str]],
    with_llm: bool,
    embedder: Any,
) -> tuple[str, _Event]:
    """Seed the finding's chain at the grounded stub and flip
    ``acquiring`` → ``tracing`` so the normal lifecycle proceeds on the
    next pass (one hop per call, matching the tracing arm's own
    discipline)."""
    from precis.store.types import Tag

    claim_text = _claim_body(conn, finding.ref_id)
    chunk_id, chunk_ord, chunk_text = _select_grounding_chunk(
        store, embedder, claim_text, stub_ref_id, chunks
    )
    ev.frontier = {"ref_id": stub_ref_id, "ord": chunk_ord, "chunk_id": chunk_id}

    hop: dict[str, Any] = {
        "ref_id": stub_ref_id,
        "chunk_id": chunk_id,
        "ord": chunk_ord,
    }
    if with_llm:
        target = _fetch_ref(conn, stub_ref_id)
        verification = _verify_support_with_caveats(
            claim=claim_text,
            scope=finding.meta.get("scope") or {},
            target_cite_key=(target or {}).get("slug") or f"ref:{stub_ref_id}",
            target_chunk_ord=chunk_ord,
            target_chunk_text=chunk_text,
        )
        if verification:
            hop["verification"] = verification
            ev.llm = {
                "hook": "verify",
                "supports": verification.get("supports"),
                "caveats_n": len(verification.get("caveats") or []),
                "cited_others_n": len(verification.get("cited_others") or []),
            }

    store.add_link(
        src_ref_id=finding.ref_id,
        dst_ref_id=stub_ref_id,
        dst_pos=chunk_ord,
        relation=_DERIVED_FROM,
        conn=conn,
    )
    store.update_ref(finding.ref_id, meta_patch={"chain": [hop]}, conn=conn)
    store.add_tag(
        finding.ref_id,
        Tag.closed(_STATUS_NAMESPACE, _TRACING),
        set_by="chase",
        replace_prefix=True,
        conn=conn,
    )
    ev.next = {"ref_id": stub_ref_id, "chunk_id": chunk_id, "ord": chunk_ord}
    ev.reason = "grounded"
    return "advanced", ev


def _advance_acquiring(
    conn: Connection,
    store: Any,
    finding: FindingRow,
    ev: _Event,
    *,
    with_llm: bool,
    embedder: Any,
) -> tuple[str, _Event]:
    """Advance an ``acquiring`` finding by at most one step.

    Polls the finding's linked ``awaits-evidence`` stubs (see
    :func:`~precis.handlers.finding.FindingHandler._put_acquiring`).
    The FIRST stub found with body chunks gets grounded (see
    :func:`_ground_on_stub`) — ``"advanced"``, flips to ``tracing``.
    While every stub is still bare, this is a no-op ``"waiting"`` pass
    UNLESS every stub is fetch-exhausted (:func:`_stub_exhausted`) past
    the acquisition-mode grace window (:func:`_acquire_grace_days`), in
    which case it's an honest ``dead_chain(reason=unacquirable)`` give-up
    (the acquisition-mode give-up rule) — never the tracing arm's
    empty-chain instant-dead (an acquiring finding is MINTED with an
    empty ``meta.chain`` by design).
    """
    stub_links = store.links_for(
        finding.ref_id, direction="out", relation=_AWAITS_EVIDENCE
    )
    if not stub_links:
        # put(kind='finding', wants=...) always links >=1 stub -- an
        # acquiring finding with none is an upstream anomaly, not a
        # legitimate "still waiting" state. Defensive dead, not an
        # infinite wait on nothing to poll.
        _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason="no_stubs")
        ev.reason = "no_stubs"
        return "dead", ev

    all_exhausted = True
    for stub_link in stub_links:
        stub_ref_id = stub_link.dst_ref_id
        stub = _fetch_ref(conn, stub_ref_id)
        if stub is None:
            continue  # soft-deleted stub -- neither blocks nor grounds
        chunks = _fetch_chunks(conn, stub_ref_id)
        if chunks:
            return _ground_on_stub(
                conn,
                store,
                finding,
                ev,
                stub_ref_id=stub_ref_id,
                chunks=chunks,
                with_llm=with_llm,
                embedder=embedder,
            )
        if not _stub_exhausted(conn, stub_ref_id):
            all_exhausted = False

    if all_exhausted:
        waits, run_age_days = _waiting_run_stats(conn, finding.ref_id)
        grace_days = _acquire_grace_days()
        if run_age_days >= grace_days:
            _set_status(conn, finding.ref_id, _DEAD_CHAIN, reason=_UNACQUIRABLE)
            ev.reason = _UNACQUIRABLE
            ev.frontier["waited_days"] = round(run_age_days, 1)
            ev.frontier["waits"] = waits
            return "dead", ev

    return "waiting", ev


# ── Runner ─────────────────────────────────────────────────────────


def run_finding_chase_pass(
    store: Any,
    *,
    limit: int = 32,
    with_llm: bool | None = None,
    taproot_enabled: bool | None = None,
    taproot_embedder: Any = None,
) -> dict[str, int]:
    """Process up to ``limit`` ``STATUS:tracing`` findings.

    Each finding runs in its own transaction so a single failure
    doesn't poison the batch. ``with_llm`` defaults to the
    ``PRECIS_CHASE_LLM`` env (truthy values turn the LLM hooks on).

    ``taproot_enabled`` (Phase-3 W1 forward bridge) defaults to the
    ``PRECIS_TAPROOT_CHASE_ENABLED`` env — independent of
    ``PRECIS_CHASE_LLM``/``with_llm``; it only has any effect on a
    finding whose pass *also* ran with the LLM verifier (see
    :func:`advance_finding`). ``taproot_embedder`` is threaded to
    :func:`_taproot_bridge` for ``canon.block``'s ANN lookup — the caller
    (``cli/worker.py``) constructs it once per boot; ``None`` degrades
    the bridge to a no-op rather than erroring.

    Returns a dict suitable for ``BatchResult`` aggregation:
    ``{claimed, ok, failed}``. The expanded counts
    (advanced/terminated/dead/...) are visible in DEBUG logs.
    """
    if with_llm is None:
        with_llm = bool(int(os.environ.get("PRECIS_CHASE_LLM", "0") or "0"))
    if taproot_enabled is None:
        taproot_enabled = bool(int(os.environ.get(_TAPROOT_CHASE_ENV, "0") or "0"))

    # Stage 1: claim under a short-lived tx.
    with store.pool.connection() as conn:
        findings = claim_tracing_findings(conn, limit=limit)
        # The SKIP LOCKED claim holds row locks until commit; we want
        # the rows but not the locks (we'll touch each in its own tx).
        # The cleanest release is committing the (empty-write)
        # transaction here.

    result = PassResult(claimed=len(findings))
    for finding in findings:
        t0 = time.perf_counter()
        try:
            with store.pool.connection() as conn:
                outcome, ev = advance_finding(
                    conn,
                    store,
                    finding,
                    with_llm=with_llm,
                    taproot_enabled=taproot_enabled,
                    taproot_embedder=taproot_embedder,
                )
                duration_ms = int((time.perf_counter() - t0) * 1000)
                _flush_event(store, conn, finding.ref_id, outcome, ev, duration_ms)
                conn.commit()
            field = _OUTCOME_FIELD[outcome]
            setattr(result, field, getattr(result, field) + 1)
        except Exception as exc:  # pragma: no cover — defensive
            duration_ms = int((time.perf_counter() - t0) * 1000)
            log.warning(
                "chase: ref_id=%s failed: %s", finding.ref_id, exc, exc_info=True
            )
            try:
                ev = _Event(decision="failed", error=str(exc)[:400])
                store.append_event(
                    finding.ref_id,
                    source=_SOURCE,
                    event="failed",
                    payload=_event_payload(ev),
                    duration_ms=duration_ms,
                )
            except Exception:  # pragma: no cover — event log itself failed
                log.warning("chase: failed to record failure event", exc_info=True)
            result.failed += 1

    ok = (
        result.advanced
        + result.terminated
        + result.dead
        + result.multi
        + result.cycled
        + result.waiting
    )
    log.debug(
        "chase: claimed=%d advanced=%d terminated=%d dead=%d "
        "multi=%d cycled=%d waiting=%d failed=%d",
        result.claimed,
        result.advanced,
        result.terminated,
        result.dead,
        result.multi,
        result.cycled,
        result.waiting,
        result.failed,
    )
    return {"claimed": result.claimed, "ok": ok, "failed": result.failed}


_OUTCOME_FIELD = {
    "advanced": "advanced",
    "terminated": "terminated",
    "dead": "dead",
    "multi": "multi",
    "cycle": "cycled",
    "waiting": "waiting",
}


# ── Event flush ────────────────────────────────────────────────────


def _event_payload(ev: _Event) -> dict[str, Any]:
    """Project an _Event into the JSONB payload shape.

    Strips empty / None fields so the row stays compact. The
    ``decision`` field rides in the ``event`` column on the
    ref_events row, not the payload, so it's omitted here.
    """
    out: dict[str, Any] = {}
    if ev.frontier:
        out["frontier"] = ev.frontier
    if ev.next is not None:
        out["next"] = ev.next
    if ev.reason is not None:
        out["reason"] = ev.reason
    if ev.inline_cites_detected:
        out["inline_cites_detected"] = ev.inline_cites_detected
    if ev.llm is not None:
        out["llm"] = ev.llm
    if ev.error is not None:
        out["error"] = ev.error
    return out


def _flush_event(
    store: Any,
    conn: Connection,
    ref_id: int,
    outcome: str,
    ev: _Event,
    duration_ms: int,
) -> None:
    """Write one ref_events row for the just-completed chase pass.

    Participates in the same transaction as the chase mutations so
    a single COMMIT writes both atomically.
    """
    store.append_event(
        ref_id,
        source=_SOURCE,
        event=outcome,
        payload=_event_payload(ev),
        duration_ms=duration_ms,
        cost_usd=ev.cost_usd,
        conn=conn,
    )


# ── Internals ──────────────────────────────────────────────────────


def _fetch_ref(conn: Connection, ref_id: int) -> dict[str, Any] | None:
    """Minimal ref-row fetch (id, slug, kind, deleted, identifiers JSONB)."""
    row = conn.execute(
        """
        SELECT r.ref_id,
               -- min(): a ref can carry >1 cite_key (PK is (id_kind,id_value),
               -- not (ref_id,id_kind)), so a bare scalar subquery raises
               -- CardinalityViolation on a dedup-merged ref.
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS slug,
               r.deleted_at,
               COALESCE(
                 (SELECT jsonb_object_agg(id_kind, id_value)
                    FROM ref_identifiers WHERE ref_id = r.ref_id),
                 '{}'::jsonb
               ) AS identifiers,
               r.kind
          FROM refs r
         WHERE r.ref_id = %s
        """,
        (ref_id,),
    ).fetchone()
    if row is None or row[2] is not None:
        return None
    return {
        "ref_id": int(row[0]),
        "slug": row[1],
        "identifiers": dict(row[3] or {}),
        "kind": row[4],
    }


def _fetch_chunks(conn: Connection, ref_id: int) -> list[tuple[int, int, str]]:
    """Body chunks (ord >= 0) for ``ref_id``: list of ``(chunk_id, ord, text)``."""
    rows = conn.execute(
        "SELECT chunk_id, ord, text FROM chunks "
        "WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
        (ref_id,),
    ).fetchall()
    return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]


def _waiting_run_stats(conn: Connection, ref_id: int) -> tuple[int, float]:
    """Stats for the *current* consecutive-``waiting`` run of a finding.

    Returns ``(waits, age_days)`` where ``waits`` is the number of
    ``waiting`` chase events since the last non-waiting outcome (the
    same run the claim's exponential backoff counts) and ``age_days``
    is the wall-clock age, in days, of the *oldest* ``waiting`` event in
    that run. A finding with no prior ``waiting`` events returns
    ``(0, 0.0)``. Age — not count — is what gates the terminal give-up
    so a short, dense spin-loop burst can't trip it (see
    :data:`WAITING_ABANDON_AFTER_DAYS`).
    """
    row = conn.execute(
        """
        WITH boundary AS (
            SELECT COALESCE(
                (SELECT max(ts) FROM ref_events
                  WHERE ref_id = %(rid)s AND source = %(src)s
                    AND event <> 'waiting'),
                '-infinity'::timestamptz
            ) AS since
        )
        SELECT count(*)::int AS waits,
               COALESCE(
                   EXTRACT(EPOCH FROM (now() - min(e.ts))) / 86400.0,
                   0.0
               ) AS age_days
          FROM ref_events e, boundary b
         WHERE e.ref_id = %(rid)s AND e.source = %(src)s
           AND e.event = 'waiting' AND e.ts > b.since
        """,
        {"rid": ref_id, "src": _SOURCE},
    ).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0] or 0), float(row[1] or 0.0)


def _claim_body(conn: Connection, ref_id: int) -> str:
    """Read the finding_body chunk text for ``ref_id``."""
    row = conn.execute(
        "SELECT text FROM chunks "
        "WHERE ref_id = %s AND chunk_kind = 'finding_body' "
        "ORDER BY ord LIMIT 1",
        (ref_id,),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _select_target_chunk(
    chunks: list[tuple[int, int, str]],
    frontier_ord: int | None,
    finding: FindingRow,
    *,
    with_llm: bool,
) -> tuple[int, int, str] | None:
    """Pick the chunk in the target paper to walk from.

    Deterministic: if ``frontier_ord`` is set, use it; otherwise
    take the chunk with the highest lexical overlap with the
    finding's title (simple unigram match; ANN would be better but
    requires embedder access from the worker — defer).

    With ``--with-llm``, the LLM verifier confirms the pick or
    proposes an alternate ord.
    """
    if frontier_ord is not None:
        match = next((c for c in chunks if c[1] == frontier_ord), None)
        if match is not None:
            return match
        # Frontier ord was specified but no longer exists (re-ingest
        # renumbered the chunks). Fall through to lexical pick.

    title_tokens = _tokenize(finding.title)
    if not title_tokens:
        return chunks[0] if chunks else None

    best = max(chunks, key=lambda c: _overlap(title_tokens, _tokenize(c[2])))
    if with_llm:
        confirmed = _locate_chunk_in_target(
            claim=finding.title,
            proposed=best,
            alternates=[c for c in chunks if c[0] != best[0]][:3],
        )
        if confirmed is not None:
            return confirmed
    return best


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"\w+", text) if len(w) > 2}


def _overlap(a: set[str], b: set[str]) -> int:
    return len(a & b)


def _detect_inline_cites(chunk_text: str) -> list[str]:
    """Extract inline citation tokens from a chunk.

    Returns the captured cite tokens (e.g. ``["[12]", "[5,7]"]``
    or ``["(Miller 2020)"]``). Empty list means no inline cites
    were detected → chunk is a candidate terminal.
    """
    hits: list[str] = []
    for m in _NUMBERED_RE.finditer(chunk_text):
        hits.append(m.group(0))
    for m in _AUTHOR_YEAR_RE.finditer(chunk_text):
        hits.append(m.group(0))
    return hits


@dataclass(frozen=True)
class _NextHopTarget:
    """Resolved next-hop reference (doi / arxiv / s2 / cite_key plus title)."""

    doi: str | None
    arxiv: str | None
    s2_id: str | None
    title: str
    year: int | None
    #: Mint-time S2 metadata patch (see :func:`~precis.ingest.
    #: semantic_scholar.s2_stub_meta`) — set only when the source S2 ref
    #: dict carried abstract/fields/citation_count beyond the bare
    #: doi/arxiv/s2_id/title/year narrowed above; merged into a freshly
    #: minted stub's ``refs.meta`` by :func:`_resolve_or_create_stub`.
    s2_meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class _MultiCandidate:
    """Sentinel returned when >1 reference plausibly matches."""

    candidates: list[_NextHopTarget]


def _pick_next_hop(
    *,
    inline_cites: list[str],
    s2_references: list[dict[str, Any]] | None,
    with_llm: bool,
    chunk_text: str,
) -> _NextHopTarget | _MultiCandidate | None:
    """Resolve inline cites to the single next-hop reference.

    Numbered bracket form ``[12]`` indexes into ``s2_references``
    (1-based — S2's order matches the bibliography order).
    Author-year form fuzzy-matches against ``s2_references[*].title``
    / ``year`` (cheap substring match; defer better matching).

    Multi-candidate: return :class:`_MultiCandidate` so the caller
    can tag the finding and stop. With ``with_llm``, an LLM
    disambiguation pass can collapse it to a single pick.
    """
    if not inline_cites or not s2_references:
        return None

    # Aggregate all numbered refs cited in the chunk.
    candidates: list[_NextHopTarget] = []
    seen: set[int] = set()
    for token in inline_cites:
        nums = _NUMBERED_RE.findall(token)
        for grp in nums:
            for n_str in grp.split(","):
                n = int(n_str.strip())
                if n in seen or n < 1 or n > len(s2_references):
                    continue
                seen.add(n)
                ref = s2_references[n - 1]
                candidates.append(_ref_to_target(ref))

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    if with_llm:
        pick = _disambiguate_candidates(chunk_text, candidates)
        if pick is not None and 0 <= pick < len(candidates):
            return candidates[pick]

    return _MultiCandidate(candidates=candidates)


def _ref_to_target(s2_ref: dict[str, Any]) -> _NextHopTarget:
    # ``s2_ref`` is a ``_load_s2_references`` entry — usually the narrow
    # doi/s2_id/title/year shape from ``ingest.citations._get_references``,
    # but build the mint-time S2 patch whenever it happens to carry more
    # (abstract/fields/citation_count) rather than assuming it never does.
    from precis.ingest.semantic_scholar import s2_stub_meta_if_present

    s2_meta = s2_stub_meta_if_present(s2_ref, now=datetime.now(UTC))
    return _NextHopTarget(
        doi=(s2_ref.get("doi") or None) or None,
        arxiv=None,
        s2_id=(s2_ref.get("s2_id") or None) or None,
        title=str(s2_ref.get("title") or ""),
        year=s2_ref.get("year"),
        s2_meta=s2_meta,
    )


def _s2_paper_id(identifiers: dict[str, Any]) -> str | None:
    """Build an S2-queryable paper id from a ref's ``ref_identifiers`` dict."""
    if identifiers.get("doi"):
        return f"doi:{identifiers['doi']}"
    if identifiers.get("arxiv"):
        return f"arxiv:{identifiers['arxiv']}"
    if identifiers.get("s2"):
        return str(identifiers["s2"])
    return None


def load_s2_citation_graph(identifiers: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch a paper's full S2 citation-graph result: ``references`` +
    ``cited_by`` in one call (:func:`precis.ingest.citations.citations`).

    Public (no leading underscore) because it's shared beyond this
    module: :func:`_load_s2_references` below uses the outbound half;
    ``workers/inbound_chase.py`` uses the ``cited_by`` half — previously
    fetched and silently discarded by :func:`_load_s2_references`. One S2
    call per paper now serves both directions instead of two.

    Returns ``None`` on S2 failure (rate-limit, network, no usable
    identifier) — callers treat this as "can't resolve" and retry next
    pass.
    """
    # Lazy import: ``semanticscholar`` ships in the ``[paper]`` extra, but
    # this module is on the ``get(kind='paper')`` read path (via
    # ``inbound_chase.mark_paper_active``), which must work in venvs
    # without that extra (asa's /opt/asa). Same pattern as
    # ``watch_poll.py`` / ``backfill/citation_lens.py``.
    from precis.ingest.citations import citations as fetch_s2_citations

    paper_id = _s2_paper_id(identifiers)
    if paper_id is None:
        return None
    try:
        return fetch_s2_citations(paper_id)
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("chase: S2 lookup failed for %s: %s", paper_id, exc)
        return None


def _load_s2_references(identifiers: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Fetch the source paper's S2 references list (ordered).

    Returns ``None`` on S2 failure (rate-limit, network) — the chase
    treats this as "can't resolve" and leaves the finding tracing
    for the next pass.
    """
    result = load_s2_citation_graph(identifiers)
    if result is None:
        return None
    refs = result.get("references")
    return refs if isinstance(refs, list) else None


def _resolve_or_create_stub(
    conn: Connection, store: Any, target: _NextHopTarget
) -> int | None:
    """Resolve a next-hop target to a ref_id, creating a stub if needed.

    Returns ``None`` when the target has no usable external ID
    (caller will tag dead_chain). Otherwise returns the ref_id of
    the existing or freshly-minted ref.
    """
    # Probe existing refs by every identifier we have.
    from precis.identity import normalize_doi

    probes: list[tuple[str, str]] = []
    if target.doi:
        # Canonicalise (lowercase + strip doi:/URL prefixes) so the probe and
        # the minted stub row match the trigger-lowercased storage form.
        norm_doi = normalize_doi(target.doi)
        if norm_doi:
            probes.append(("doi", norm_doi))
    if target.arxiv:
        probes.append(("arxiv", target.arxiv))
    if target.s2_id:
        probes.append(("s2", target.s2_id))
    if not probes:
        return None  # No external ID → no stub (per design).

    for id_kind, id_value in probes:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
            (id_kind, id_value),
        ).fetchone()
        if row is not None:
            return int(row[0])

    # No hit — mint a stub. The chase actor pins set_by='chase' so
    # the audit trail surfaces every chase-created ref.
    from precis.identity import make_cite_key

    cite_key = make_cite_key(
        [{"family": _first_word(target.title) or "anon"}],
        target.year,
        taken=set(),  # collision resolution deferred to a follow-up
    )
    title = target.title or "(no title)"
    stub_meta: dict[str, Any] = {**(target.s2_meta or {}), "set_by": "chase"}
    new_ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=title,
        meta=stub_meta,
        conn=conn,
    )
    # Register every external ID we have, plus the chase-actor row.
    for id_kind, id_value in probes:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (id_kind, id_value, new_ref.id, "chase"),
        )
    return int(new_ref.id)


def _first_word(s: str) -> str | None:
    """Cheap title → surname fallback. Used as a last-ditch cite_key
    seed when S2 doesn't return author info.
    """
    if not s:
        return None
    m = re.search(r"[A-Za-z]+", s)
    return m.group(0).lower() if m else None


def _record_candidates(
    conn: Connection,
    store: Any,
    finding_ref_id: int,
    candidates: list[_NextHopTarget],
) -> None:
    """Persist multi-candidate refs as `derived-from candidate=true` links.

    The user resolves by calling ``edit(kind='finding', id=N,
    pick_candidate='<cite_key>')`` (deferred verb — for now the
    candidates surface via ``get(kind='finding')`` rendering).
    """
    for cand in candidates:
        ref_id = _resolve_or_create_stub(conn, store, cand)
        if ref_id is None:
            continue
        store.add_link(
            src_ref_id=finding_ref_id,
            dst_ref_id=ref_id,
            dst_pos=None,
            relation=_DERIVED_FROM,
            meta={"candidate": True},
            conn=conn,
        )


def _aggregate_caveats(chain: list[dict[str, Any]]) -> list[str]:
    """Caveats from every hop's ``verification.caveats``, deduped,
    order-preserved. Shared by :func:`_snapshot_chain` (``meta.caveats``)
    and :func:`_taproot_bridge` (the evidence-edge ``meta.caveats``), so
    the two stay in lockstep rather than drifting apart."""
    aggregated: list[str] = []
    seen: set[str] = set()
    for hop in chain:
        v = hop.get("verification") or {}
        for c in v.get("caveats") or []:
            c_str = str(c).strip()
            if c_str and c_str not in seen:
                seen.add(c_str)
                aggregated.append(c_str)
    return aggregated


def _evidence_edge_meta(
    verification: dict[str, Any] | None,
    chain: list[dict[str, Any]],
    *,
    slug: str,
    ord_: int | None,
) -> dict[str, Any]:
    """Map one hop's ``verification`` dict to a taproot evidence edge's
    ``meta`` shape. Shared by :func:`_taproot_bridge`'s terminal write
    (W1) and its intermediate-hop corroborator attach (W2), so the two
    stay in lockstep rather than drifting apart.

    ``caveats`` is always the whole-chain aggregate (:func:`_aggregate_caveats`)
    -- the same value on every edge for a given chain -- while
    ``source_handle`` is per-hop (``slug``/``ord_`` are the attaching
    hop's own, not the terminal's). ``verification=None`` (a hop that was
    never LLM-verified, e.g. an untouched intermediate frontier) yields a
    minimal meta with ``support``/``support_reason`` left ``None`` rather
    than fabricating a verdict.
    """
    v = verification or {}
    return {
        "support": v.get("supports"),
        "support_reason": v.get("support_reason"),
        "caveats": _aggregate_caveats(chain),
        "char_offset": None,  # no producer yet (deferred)
        "source_handle": f"{slug}~{ord_}" if ord_ is not None else slug,
    }


def _snapshot_chain(
    conn: Connection, store: Any, finding_ref_id: int, chain: list[dict[str, Any]]
) -> None:
    """Run the chain-snapshot pass at chain termination.

    Per finding-chase.md §"Chain-snapshot pass":
    1. ``meta.primary_cite_key`` = cite_key of the last chain entry.
    2. ``meta.via_cite_keys`` = ordered cite_keys of intermediate
       entries (excluding the finding's initial cite AND the primary).
    3. ``meta.caveats`` = aggregated caveats from every hop's
       ``verification.caveats`` (deduped, order-preserved).
    4. Re-emit ``card_combined`` via DELETE+INSERT.
    5. Flip ``STATUS:tracing`` → ``STATUS:established``.

    No LLM call here — pure text concat + DB writes.
    """
    cite_keys: list[str] = []
    for hop in chain:
        ref = _fetch_ref(conn, int(hop["ref_id"]))
        slug = ref["slug"] if ref else None
        cite_keys.append(slug or f"ref:{hop['ref_id']}")

    primary_cite_key = cite_keys[-1] if cite_keys else None
    # via = intermediate hops, excluding the initial cite (first)
    # AND the primary (last). For a 2-hop chain (initial → primary)
    # this is empty.
    via_cite_keys = cite_keys[1:-1] if len(cite_keys) > 2 else []

    aggregated_caveats = _aggregate_caveats(chain)

    # Patch the finding's meta.
    store.update_ref(
        finding_ref_id,
        meta_patch={
            "chain": chain,
            "primary_cite_key": primary_cite_key,
            "via_cite_keys": via_cite_keys,
            "caveats": aggregated_caveats,
        },
        conn=conn,
    )

    # Re-emit card_combined. DELETE the old row so the embedding row
    # cascades away → next embed pass will re-embed the new card.
    conn.execute(
        "DELETE FROM chunks WHERE ref_id = %s AND ord = -1",
        (finding_ref_id,),
    )
    # Pull the finding title for the card text.
    title_row = conn.execute(
        "SELECT title FROM refs WHERE ref_id = %s", (finding_ref_id,)
    ).fetchone()
    title = title_row[0] if title_row else ""
    via_str = ",".join(via_cite_keys)
    card_text = (
        f"{title} [primary={primary_cite_key}; via={via_str}]"
        if via_cite_keys
        else f"{title} [primary={primary_cite_key}]"
    )
    conn.execute(
        "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
        "VALUES (%s, %s, %s, %s, %s::jsonb)",
        (finding_ref_id, -1, "card_combined", card_text, "{}"),
    )

    # Flip the status tag.
    from precis.store.types import Tag

    store.add_tag(
        finding_ref_id,
        Tag.closed(_STATUS_NAMESPACE, _ESTABLISHED),
        set_by="chase",
        replace_prefix=True,
        conn=conn,
    )


def _taproot_bridge(
    conn: Connection,
    store: Any,
    finding: FindingRow,
    *,
    chain: list[dict[str, Any]],
    verification: dict[str, Any],
    target: dict[str, Any],
    chunk_ord: int,
    embedder: Any,
) -> None:
    """Phase-3 W1 forward bridge: mint/attach a taproot claim hub for a
    finding that just established, off the terminal hop's LLM verdict.

    Called from :func:`advance_finding` right after :func:`_snapshot_chain`,
    on the SAME ``conn`` — so the hub/edge write lands in the same
    transaction as the ``STATUS:established`` flip. The
    ``block``/``dedup_judge``/``place`` → :func:`~precis.taproot.hub.apply_placement`
    write is wrapped in a nested transaction (savepoint): a taproot failure
    (bad embedder, dispatch error, a genuine bug) rolls back only the
    taproot write, never the surrounding established-flip -- the chase's
    deterministic outcome must never depend on taproot's health.

    Exception: a ``needs_review`` :class:`~precis.taproot.canon.Placement`
    files a ``kind='todo'`` via ``_todo_fn`` -> :func:`_file_taproot_review_todo`,
    which deliberately does NOT ride ``conn``/this savepoint -- it opens
    and commits its own ``store.tx()``. Filing the review todo is a
    side-effect for a human, not part of the atomic evidence write (there
    is no hub/edge write to keep in lockstep with on a ``needs_review``
    placement in the first place), so it stands on its own regardless of
    what else this savepoint or the outer established-flip transaction
    does.

    Skips (no hub, no edge, no LLM canon calls) on:
    * no embedder available (``block`` needs ``.embed_one`` — degrade
      gracefully rather than crash the chase);
    * ``verification["supports"] == "no"`` (NO-SUPPORT: never record a
      non-supporting paper as evidence);
    * an empty finding title (the NO-CLAIM equivalent here — see the
      module-level note on why this doesn't call
      :func:`~precis.taproot.canon.extract_claim`).
    """
    if embedder is None:
        log.info(
            "taproot: bridge skipped for finding ref_id=%s (no embedder)",
            finding.ref_id,
        )
        return

    supports = verification.get("supports")
    if supports == "no":
        return

    # The finding's own title *is* the claim -- chase findings are
    # already user-asserted claims being traced to a source, unlike a raw
    # paper chunk that may or may not assert anything. So this builds the
    # CanonicalClaim directly rather than routing it through
    # canon.extract_claim's "does this passage assert a claim?" LLM gate
    # (which exists for untrusted chunk text, not an already-a-claim
    # finding) -- an empty title is the analogous NO-CLAIM skip.
    sentence = (finding.title or "").strip()
    if not sentence:
        return
    scope = {
        str(k): str(v)
        for k, v in (finding.meta.get("scope") or {}).items()
        if v is not None
    }
    claim = CanonicalClaim(sentence=sentence, scope=scope)

    try:
        candidates = block(claim, store, embedder)
        judged = [
            (cand, dedup_judge(claim.sentence, cand.claim)) for cand in candidates
        ]
        placement = place(claim, judged)
    except Exception:
        log.warning(
            "taproot: bridge canonicalization failed for finding ref_id=%s",
            finding.ref_id,
            exc_info=True,
        )
        return

    paper_ref_id = int(chain[-1]["ref_id"])
    paper_slug = target.get("slug") or f"ref:{paper_ref_id}"
    edge_meta = _evidence_edge_meta(
        verification, chain, slug=paper_slug, ord_=chunk_ord
    )

    def _todo_fn(claim: CanonicalClaim, placement: Placement) -> None:
        _file_taproot_review_todo(
            store, claim, placement, finding_ref_id=finding.ref_id
        )

    try:
        with conn.transaction():  # savepoint: isolate a taproot write failure
            hub_ref_id = apply_placement(
                store,
                claim,
                placement,
                paper_ref_id=paper_ref_id,
                meta=edge_meta,
                todo_fn=_todo_fn,
                set_by="chase",
                conn=conn,
            )
            if hub_ref_id is not None:  # None only on needs_review (no hub)
                _attach_intermediate_corroborators(
                    conn,
                    store,
                    chain=chain,
                    hub_ref_id=hub_ref_id,
                    terminal_ref_id=paper_ref_id,
                )
    except Exception:
        log.warning(
            "taproot: bridge apply_placement failed for finding ref_id=%s",
            finding.ref_id,
            exc_info=True,
        )


def _attach_intermediate_corroborators(
    conn: Connection,
    store: Any,
    *,
    chain: list[dict[str, Any]],
    hub_ref_id: int,
    terminal_ref_id: int,
) -> None:
    """Phase-3 W2: attach every INTERMEDIATE chain hop (every entry but
    the terminal ``chain[-1]``, which the caller already attached per W1)
    that is a live paper as a ``corroborates`` evidence edge on the same
    hub -- giving the hub a real multi-supporter set so
    :func:`~precis.taproot.seniority.derive_evidence` can actually split
    establishes vs corroborators instead of degenerating to a
    single-supporter no-op.

    Called from inside the SAME savepoint as the terminal attach (the
    caller's ``conn.transaction()``) -- one taproot write failure here
    rolls back the whole bridge write for this finding, never the
    surrounding established-flip. ``role`` is always written
    ``'corroborates'`` -- :mod:`~precis.taproot.seniority` derives
    establishes at *read* time from the ``cites`` graph; W2 never guesses
    it at write time.

    A hop is skipped, not attached, when it:

    * duplicates the terminal paper, the hub itself, or an earlier hop in
      this same chain (de-dup via ``seen`` -- ``add_link``'s own
      ``ON CONFLICT`` would no-op a repeat anyway, but skipping avoids the
      redundant write and log noise);
    * isn't a live ``kind='paper'`` ref (defensive -- the chain should
      only ever hold papers, but a finding/hub/soft-deleted ref must
      never become taproot evidence);
    * carries ``verification['supports'] == 'no'`` (NO-SUPPORT is never
      evidence, mirroring the terminal's own skip).

    A hop with no ``verification`` at all (never LLM-verified -- e.g. an
    intermediate frontier the chain grew past deterministically, or the
    initial ``cited_in`` hop) still gets attached, as a bare corroborator
    with a minimal meta (:func:`_evidence_edge_meta` called with
    ``verification=None``) -- the goal here is supporter *membership* for
    the seniority split, not a support verdict this hop never produced.
    """
    seen: set[int] = {terminal_ref_id, hub_ref_id}
    for hop in chain[:-1]:  # every hop but the terminal (chain[-1], W1)
        hop_ref_id = int(hop["ref_id"])
        if hop_ref_id in seen:
            continue
        seen.add(hop_ref_id)

        hop_ref = _fetch_ref(conn, hop_ref_id)
        if hop_ref is None or hop_ref["kind"] != "paper":
            continue  # deleted, missing, or not a paper -- never evidence

        hop_verification = hop.get("verification")
        if hop_verification and hop_verification.get("supports") == "no":
            continue  # NO-SUPPORT: never record as evidence

        hop_slug = hop_ref["slug"] or f"ref:{hop_ref_id}"
        hop_meta = _evidence_edge_meta(
            hop_verification, chain, slug=hop_slug, ord_=hop.get("ord")
        )
        attach_evidence(
            store,
            hub_ref_id=hub_ref_id,
            paper_ref_id=hop_ref_id,
            role="corroborates",
            meta=hop_meta,
            set_by="chase",
            conn=conn,
        )


def _file_taproot_review_todo(
    store: Any,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    finding_ref_id: int,
) -> None:
    """Minimal ``kind='todo'`` for a risky (``needs_review``) taproot merge
    the bridge declined to auto-apply (taproot.md open #16).

    Deliberately its own ``store.tx()``, NOT the ``conn`` the rest of
    :func:`_taproot_bridge` threads through — see that function's
    docstring. There is no hub/edge write on the ``needs_review`` path to
    stay atomic with, so filing the todo stands alone rather than riding
    the bridge's savepoint.
    """
    from precis.store.types import Tag

    title = f"taproot: review merge for finding ref_id={finding_ref_id}"
    with store.tx() as c:
        todo = store.insert_ref(
            kind="todo",
            slug=None,
            title=title[:200],
            meta={
                "source": "taproot:chase",
                "finding_ref_id": finding_ref_id,
                "claim_sentence": claim.sentence,
                "placement_reason": placement.reason,
                "candidate_hub_ref_id": placement.hub_ref_id,
            },
            conn=c,
        )
        store.add_tag(
            todo.id,
            Tag.closed(_STATUS_NAMESPACE, "open"),
            set_by="chase",
            replace_prefix=True,
            conn=c,
        )


def _set_status(
    conn: Connection,
    finding_ref_id: int,
    value: str,
    *,
    reason: str | None = None,
) -> None:
    """Replace the STATUS tag and (optionally) record a dead-chain reason."""
    from precis.store.types import Tag

    # Need a store handle for tags; this module is called with one
    # via advance_finding but _set_status is a leaf helper so we
    # inline the SQL to avoid threading store through.
    # Drop the existing STATUS tag first (replace_prefix doesn't run
    # over a raw conn).
    conn.execute(
        """
        DELETE FROM ref_tags
         USING tags
         WHERE ref_tags.tag_id = tags.tag_id
           AND ref_tags.ref_id = %s
           AND tags.namespace = %s
        """,
        (finding_ref_id, _STATUS_NAMESPACE),
    )
    # Upsert the new tag and link it.
    new_tag_row = conn.execute(
        "INSERT INTO tags (namespace, value) VALUES (%s, %s) "
        "ON CONFLICT (namespace, value) DO UPDATE SET namespace = EXCLUDED.namespace "
        "RETURNING tag_id",
        (_STATUS_NAMESPACE, value),
    ).fetchone()
    assert new_tag_row is not None
    conn.execute(
        "INSERT INTO ref_tags (ref_id, tag_id, set_by) "
        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (finding_ref_id, int(new_tag_row[0]), "chase"),
    )
    if reason:
        conn.execute(
            "UPDATE refs SET meta = meta || %s, updated_at = now() WHERE ref_id = %s",
            (Jsonb({"dead_reason": reason}), finding_ref_id),
        )
    # Tag is imported above for type-checker visibility; the SQL
    # path above doesn't use it directly but keeps the symbol
    # available for future refactors that route via store.add_tag.
    _ = Tag


# LLM hooks (``_verify_support_with_caveats``,
# ``_disambiguate_candidates``, ``_locate_chunk_in_target``) and their
# prompts moved to ``precis.workers._chase_llm`` 2026-06-05. They are
# imported at the top of this file so the call-site in
# ``advance_finding`` is unchanged. The default-off contract is
# preserved: the hooks only execute when ``with_llm=True`` (or
# ``PRECIS_CHASE_LLM=1``) reaches the call site.


__all__ = [
    "FindingRow",
    "PassResult",
    "advance_finding",
    "claim_tracing_findings",
    "load_s2_citation_graph",
    "run_finding_chase_pass",
]
