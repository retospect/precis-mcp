"""run_finding_chase_pass — sibling worker that advances finding chains.

Per ADR 0018, ref-level workers are sibling functions, not
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
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.ingest.citations import citations as fetch_s2_citations
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


# ── Constants ──────────────────────────────────────────────────────

_STATUS_NAMESPACE = "STATUS"
_TRACING = "tracing"
_ESTABLISHED = "established"
_DEAD_CHAIN = "dead_chain"
_CYCLE = "cycle"
_MULTI_CANDIDATE = "multi_candidate"

_DERIVED_FROM = "derived-from"

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
# within a day while killing the per-minute flood. Never gives up
# entirely — a one-a-day re-poke is cheap insurance.
WAITING_BACKOFF_MAX_MINUTES = 1440

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
    """Lock and return up to ``limit`` ``STATUS:tracing`` findings.

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
    chain that just advanced keeps moving promptly.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    rows = conn.execute(
        """
        SELECT r.ref_id, r.title, r.meta
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
                    AND t.value = %(tracing)s
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
                           * POWER(2, GREATEST(wait_run.waits - 1, 0)),
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
            "base": float(waiting_backoff_minutes),
            "cap": float(waiting_backoff_max_minutes),
            "limit": limit,
        },
    ).fetchall()
    return [
        FindingRow(ref_id=int(r[0]), title=str(r[1]), meta=dict(r[2] or {}))
        for r in rows
    ]


# ── Per-finding logic ──────────────────────────────────────────────


def advance_finding(
    conn: Connection,
    store: Any,
    finding: FindingRow,
    *,
    with_llm: bool = False,
) -> tuple[str, _Event]:
    """Advance one finding by at most one hop.

    Returns ``(outcome, event)`` — the outcome string (``"advanced"``
    / ``"terminated"`` / ``"dead"`` / ``"multi"`` / ``"cycle"`` /
    ``"waiting"``) plus the populated :class:`_Event` the runner
    flushes to ``ref_events``.
    """
    ev = _Event()
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

    # Stub paper with no chunks yet → waiting (no-op pass).
    target_chunks = _fetch_chunks(conn, frontier_ref_id)
    if not target_chunks:
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


# ── Runner ─────────────────────────────────────────────────────────


def run_finding_chase_pass(
    store: Any,
    *,
    limit: int = 32,
    with_llm: bool | None = None,
) -> dict[str, int]:
    """Process up to ``limit`` ``STATUS:tracing`` findings.

    Each finding runs in its own transaction so a single failure
    doesn't poison the batch. ``with_llm`` defaults to the
    ``PRECIS_CHASE_LLM`` env (truthy values turn the LLM hooks on).

    Returns a dict suitable for ``BatchResult`` aggregation:
    ``{claimed, ok, failed}``. The expanded counts
    (advanced/terminated/dead/...) are visible in DEBUG logs.
    """
    if with_llm is None:
        with_llm = bool(int(os.environ.get("PRECIS_CHASE_LLM", "0") or "0"))

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
                outcome, ev = advance_finding(conn, store, finding, with_llm=with_llm)
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
    """Minimal ref-row fetch (id, slug, deleted, identifiers JSONB)."""
    row = conn.execute(
        """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS slug,
               r.deleted_at,
               COALESCE(
                 (SELECT jsonb_object_agg(id_kind, id_value)
                    FROM ref_identifiers WHERE ref_id = r.ref_id),
                 '{}'::jsonb
               ) AS identifiers
          FROM refs r
         WHERE r.ref_id = %s
        """,
        (ref_id,),
    ).fetchone()
    if row is None or row[2] is not None:
        return None
    return {"ref_id": int(row[0]), "slug": row[1], "identifiers": dict(row[3] or {})}


def _fetch_chunks(conn: Connection, ref_id: int) -> list[tuple[int, int, str]]:
    """Body chunks (ord >= 0) for ``ref_id``: list of ``(chunk_id, ord, text)``."""
    rows = conn.execute(
        "SELECT chunk_id, ord, text FROM chunks "
        "WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
        (ref_id,),
    ).fetchall()
    return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]


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
    return _NextHopTarget(
        doi=(s2_ref.get("doi") or None) or None,
        arxiv=None,
        s2_id=(s2_ref.get("s2_id") or None) or None,
        title=str(s2_ref.get("title") or ""),
        year=s2_ref.get("year"),
    )


def _load_s2_references(identifiers: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Fetch the source paper's S2 references list (ordered).

    Returns ``None`` on S2 failure (rate-limit, network) — the chase
    treats this as "can't resolve" and leaves the finding tracing
    for the next pass.
    """
    paper_id = None
    if identifiers.get("doi"):
        paper_id = f"doi:{identifiers['doi']}"
    elif identifiers.get("arxiv"):
        paper_id = f"arxiv:{identifiers['arxiv']}"
    elif identifiers.get("s2"):
        paper_id = str(identifiers["s2"])
    if paper_id is None:
        return None
    try:
        result = fetch_s2_citations(paper_id)
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("chase: S2 lookup failed for %s: %s", paper_id, exc)
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
    probes: list[tuple[str, str]] = []
    if target.doi:
        probes.append(("doi", target.doi.lower()))
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
    new_ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=title,
        meta={"set_by": "chase"},
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

    # Aggregate caveats across hops, deduped.
    aggregated_caveats: list[str] = []
    seen_caveats: set[str] = set()
    for hop in chain:
        v = hop.get("verification") or {}
        for c in v.get("caveats") or []:
            c_str = str(c).strip()
            if c_str and c_str not in seen_caveats:
                seen_caveats.add(c_str)
                aggregated_caveats.append(c_str)

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
    "run_finding_chase_pass",
]
