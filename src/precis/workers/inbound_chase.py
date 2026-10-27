"""inbound_chase — one-hop, exhaustive inbound citer sweep.

The inbound counterpart to ``workers/chase.py``'s outbound finding-chase.
Where ``chase`` walks *outbound* from a finding's own claim (paper X
cites paper Y — follow the S2 reference list forward, locate + verify
the specific supporting chunk in Y), this pass walks *inbound* from an
**activated** paper Y: who cites Y, at which chunk of the citer, and
does that chunk actually engage with Y (yes / partial / no + caveats)
— reusing ``chase``'s own ``_locate_chunk_in_target`` +
``_verify_support_with_caveats`` LLM hooks rather than reinventing
them, and ``Store.upsert_stub_paper`` (which itself "mirrors the chase
worker's stub path", see its docstring) for auto-ingesting S2-known
citers that aren't in the corpus yet.

Trigger — "active paper": a *permanent* DB marker, not a session
concept. The exact engagement event ("read via get, cited by
something, or an outbound chase landing on it as a real hop") was left
to implementation judgment; this module picks **read via get()**
(:func:`mark_paper_active`, called from
``handlers/paper.py::PaperHandler.get`` on every resolved single-paper
read) — the one engagement every paper path already funnels through
regardless of which view an agent asks for, so it needs no new call
site elsewhere. Marked via a closed ``INBOUND:pending`` → ``INBOUND:
swept`` tag transition (mirrors ``chase``'s own ``STATUS:*`` tag
convention) rather than a ``meta.processing.*`` field — there is no
existing ``meta.processing`` convention in this codebase to mirror
(checked); ref-level tags are how every other pass records permanent
per-ref state (``STATUS:tracing``, ``ROLE3:own``, …), so that's what
this follows. Once ``swept``, a paper is never re-triggered — a
deliberate no-re-sweep policy.

Cost bounds — the todo-489406 flip preconditions, both shipped:

* **Per-paper candidate cap**: the design doc rejected a chase-side
  degree cap outright ("cap only at display time"); the flip
  precondition walked that back —
  ``PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER`` (default 25,
  :func:`max_candidates_per_paper`) bounds how many S2 ``cited_by``
  candidates one activated paper ever processes, and the same knob
  floors the ``handlers/_citer_sidecar.py`` render (``min(5, knob)`` —
  the sidecar's own 5-row cap keeps the read path small regardless).
  Candidates over the
  cap are counted (``capped`` in the swept event payload) and skipped
  — never stub-minted, linked or queued.
* **Daily ceiling across papers**: ``PRECIS_INBOUND_CHASE_DAILY_LIMIT``
  (default 200, :func:`daily_limit`) bounds candidates chased per UTC
  day, persisted as an ``app_state`` counter keyed by UTC date. On
  exhaustion the pass ends cleanly and unstamped — pending papers and
  unresolved pairs resume after the rollover; already-chased pairs
  skip free (``_already_linked``) so a >ceiling paper can't livelock
  on its own prefix. A ceiling-blocked pass reports ``report_only`` so
  its locked-but-unprocessed ``claimed`` count doesn't hold the worker
  loop out of its idle sleep (gr514421).

The remaining backstop, the global spend circuit breaker ("Budget
guardrails" Piece B), has **shipped**: it lives in
``precis.budget.breaker`` — ``gate_tier`` is invoked from
``utils/llm/router.route`` before any paid tier runs (caps:
``PRECIS_BUDGET_DAILY_USD`` / ``PRECIS_BUDGET_HOURLY_USD``), with
``precis.budget.meter`` rolling the spend window. It is fleet-global,
not per-paper — which is exactly why the two chase-side bounds above
exist on top of it. Cost note for whoever sizes the ceiling:
:func:`_resolve_citer_chunk` runs a *second* ``_locate_chunk_in_target``
call per citer (below) to localize into the cited paper Y's own
chunks, not just the citer's — so the per-citer LLM *locate* cost
roughly doubles (one locate call into the citer's chunks, one more
into Y's) on top of the existing one ``_verify_support_with_caveats``
call.

Staleness — link immediately, resolve later, never re-sweep:

1. **Sweep** (``run_inbound_chase_pass``, claim query 1): one S2 call
   per activated paper Y (:func:`precis.workers.chase.load_s2_citation_graph`
   — the *same* call the outbound walk already makes; its ``cited_by``
   field used to be silently discarded). For every citer, mint-
   or-resolve it (``Store.upsert_stub_paper``) and record a **paper-
   level** ``cites`` link right away, before any chunk-level work.
2. **Follow-up** (claim query 2): once a citer stub actually has body
   chunks (whether via ``fetch_oa`` landing its PDF, or it was already
   a real corpus paper), resolve the specific citing chunk and upgrade
   to a **chunk-scoped** ``cites`` link.

Judgment call: unlike
``chase``'s exponential ``waiting``-backoff for a stub with no chunks
(built to stop an event-write flood — see ``chase.py``'s module
docstring and the ``bg_job_spin_loops`` incident), this pass writes
**no row at all** while a citer stub has no chunks yet — claim query 2
below simply excludes it via a cheap indexed ``EXISTS``, so an
unresolved stub costs one SELECT-scan row per pass, not a written one.
The flood ``chase``'s backoff exists to stop doesn't apply here, so
building a parallel backoff (or wiring the ``paper_ingested``
auto_check primitive, the other candidate mechanism) would
be solving a problem this design doesn't have; the claim query itself
*is* the "wait" mechanism, for free.

Chunk resolution always writes exactly one chunk-scoped ``cites`` link
per (citer, cited) pair once the citer has chunks — even a ``supports:
no`` or unverified locate — so claim query 2's ``NOT EXISTS`` guard is
always satisfied after one attempt. This is what makes the "one-hop,
never re-sweep" policy self-terminating without extra bookkeeping: a
``no``/unverified verdict is filtered out at *display* time (the
sidecar render), not by leaving the pair permanently unresolved and
endlessly re-claimed.

Ship dark: ``PRECIS_INBOUND_CHASE_ENABLED`` (default ``"0"``) gates
both the trigger (no tag is ever written while dark, so turning the
flag off after a period fully halts new activity — existing tags/links
are inert data, not re-processed) and the pass itself. The inbound
path's LLM calls are gated by *this* flag, not ``PRECIS_CHASE_LLM`` —
that flag's default is untouched; turning this pass on at all already
implies paying for verification, so
resolution always runs with ``with_llm=True`` once the pass is
claiming work, independent of the old outbound flag's setting.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.store.types import Tag
from precis.workers.chase import (
    _fetch_chunks,
    _locate_chunk_in_target,
    _overlap,
    _s2_paper_id,
    _tokenize,
    _verify_support_with_caveats,
    load_s2_citation_graph,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_INBOUND_NAMESPACE = "INBOUND"
_PENDING = "pending"
_SWEPT = "swept"

#: ``links.meta`` marker distinguishing inbound-chase-authored ``cites``
#: rows from any other source (a user's manual ``link(...)`` call, a
#: future type-2 similarity pass, …) — claim query 2 filters on it so
#: it only ever resolves pairs *this* pass created.
_LINK_SOURCE = "inbound_chase"

_SOURCE = "inbound_chase"  # ref_events source, mirrors chase.py's _SOURCE

#: Env knob + default for the per-paper candidate cap — the todo-489406
#: precondition for flipping ``PRECIS_INBOUND_CHASE_ENABLED``: one
#: activated paper can never fan out unbounded. The design doc rejected
#: a chase-side degree cap outright (it named no number — see module
#: docstring), so 25 is the chosen default: a landmark paper's sweep
#: tops out at ~$1.25–2.50 at the spec's ~$0.05–0.10/citer while an
#: ordinary paper still sweeps whole. The same bound also floors the
#: ``_citer_sidecar`` render (``min(5, knob)`` — the display's own
#: 5-row cap keeps the read path small regardless). ``0`` caps everything (sweep
#: stamps the paper with zero links — the kill switch short of turning
#: the flag off).
_MAX_CANDIDATES_ENV = "PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER"
_DEFAULT_MAX_CANDIDATES = 25

#: Env knob + default for the cross-paper daily ceiling — todo 489406's
#: "daily spend ceiling": a fleet-wide bound on citer candidates chased
#: per UTC day (the ``app_state`` counter is shared across hosts). 200
#: ≈ $10–20/day at the spec's ~$0.05–0.10/citer — deliberately inside
#: ``PRECIS_DAILY_COST_CEILING``'s own default (20.0) even at the top of
#: the per-citer range. ``0`` stops all chasing (nothing spends, nothing
#: stamps) — the operator kill switch below the flag.
_DAILY_LIMIT_ENV = "PRECIS_INBOUND_CHASE_DAILY_LIMIT"
_DEFAULT_DAILY_LIMIT = 200

#: ``app_state`` key prefix for the persisted daily counter, keyed by UTC
#: date — the ``_throttle``/``openalex_enrich`` persistence idiom, one
#: key per day so the counter self-resets at midnight UTC without any
#: cleanup (yesterday's key is dead weight one row deep).
_DAILY_KEY_PREFIX = "inbound_chase:daily:"


def _env_int(name: str, default: int) -> int:
    """``int`` env override, non-negative; mirrors inbound_ground's own
    ``_env_int`` (floor 0 — a negative bound is nonsense, not unlimited)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def max_candidates_per_paper() -> int:
    """``PRECIS_INBOUND_CHASE_MAX_CANDIDATES_PER_PAPER`` — citers of one
    paper the sweep ever processes (default ``_DEFAULT_MAX_CANDIDATES``).
    Candidates over the cap are counted (``_SweepResult.capped``) and
    skipped — never stub-minted, linked or queued."""
    return _env_int(_MAX_CANDIDATES_ENV, _DEFAULT_MAX_CANDIDATES)


def daily_limit() -> int:
    """``PRECIS_INBOUND_CHASE_DAILY_LIMIT`` — citer candidates all papers
    together may chase per UTC day (default ``_DEFAULT_DAILY_LIMIT``).
    When the persisted counter reaches it the pass ends cleanly,
    unstamped, and resumes after the UTC-date rollover."""
    return _env_int(_DAILY_LIMIT_ENV, _DEFAULT_DAILY_LIMIT)


def _daily_key(day: datetime) -> str:
    """``app_state`` counter key for the UTC date ``day`` falls on."""
    return f"{_DAILY_KEY_PREFIX}{day.astimezone(UTC).date().isoformat()}"


def _daily_used(conn: Connection, key: str) -> int:
    """Candidates already chased under ``key`` today (0 when unwritten
    or unparseable — a corrupt row reads as a fresh day, not a wedge)."""
    row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    if row is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def _daily_bump(conn: Connection, key: str) -> int:
    """Atomically add one candidate to ``key`` inside the caller's
    transaction and return the new count. Riding the transaction means
    a rolled-back sweep spends nothing, and the single-row upsert makes
    the ceiling fleet-wide (two workers on different hosts share the
    counter)."""
    row = conn.execute(
        "INSERT INTO app_state (key, value) VALUES (%s, '1') "
        "ON CONFLICT (key) DO UPDATE SET "
        "value = (app_state.value::int + 1)::text, updated_at = now() "
        "RETURNING value",
        (key,),
    ).fetchone()
    return int(row[0]) if row is not None else 1


@dataclass
class _DailyBudget:
    """Mutable daily-ceiling view for one pass (UTC-day keyed).

    ``remaining`` is refreshed from the atomic bump's RETURNING value on
    every :meth:`take`, so a sibling host's spend lands here too — the
    ceiling holds fleet-wide, not just per process.
    """

    ceiling: int
    remaining: int
    key: str
    #: Units this pass actually spent — the digest's ``chased`` count.
    chased: int = 0

    def take(self, conn: Connection) -> bool:
        """Spend one candidate unit; ``False`` once today's ceiling is
        reached (caller stops cleanly, leaves work unstamped)."""
        if self.remaining <= 0:
            return False
        used = _daily_bump(conn, self.key)
        self.remaining = self.ceiling - used
        self.chased += 1
        return True


def inbound_chase_enabled() -> bool:
    """``PRECIS_INBOUND_CHASE_ENABLED`` — default OFF (see module docstring)."""
    return bool(int(os.environ.get("PRECIS_INBOUND_CHASE_ENABLED", "0") or "0"))


def mark_paper_active(store: Store, ref: Any) -> None:
    """Trigger the inbound sweep the first time paper ``ref`` is read.

    No-op when the feature is dark, when ``ref`` isn't a paper, or when
    the paper already carries an ``INBOUND:*`` marker (pending or
    already swept) — the permanent, never-re-trigger contract. Called
    from ``PaperHandler.get`` (see module docstring for why "read via
    get" was picked as the engagement event).
    """
    if not inbound_chase_enabled():
        return
    if getattr(ref, "kind", None) != "paper":
        return
    if store.has_tag(ref.id, _INBOUND_NAMESPACE, _PENDING) or store.has_tag(
        ref.id, _INBOUND_NAMESPACE, _SWEPT
    ):
        return
    store.add_tag(ref.id, Tag.closed(_INBOUND_NAMESPACE, _PENDING), set_by="system")


# ── Claim queries ──────────────────────────────────────────────────


def _claim_pending_papers(conn: Connection, *, limit: int) -> list[int]:
    """Lock and return up to ``limit`` ``INBOUND:pending`` paper ref_ids."""
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.retired_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(ns)s AND t.value = %(pending)s
               )
         ORDER BY r.ref_id
         LIMIT %(limit)s
           FOR UPDATE OF r SKIP LOCKED
        """,
        {"ns": _INBOUND_NAMESPACE, "pending": _PENDING, "limit": limit},
    ).fetchall()
    return [int(r[0]) for r in rows]


def _claim_citers_needing_chunk_resolution(
    conn: Connection, *, limit: int
) -> list[tuple[int, int]]:
    """Lock and return up to ``limit`` ``(citer_ref_id, cited_ref_id)`` pairs.

    A pair qualifies when this pass already recorded the paper-level
    ``cites`` link (``meta->>'source' = 'inbound_chase'``), the citer
    now has body chunks, and no chunk-scoped ``cites`` link between the
    same two refs exists yet. See module docstring for why this SQL
    filter — not an explicit backoff — is the whole "wait for the stub
    to land" mechanism.
    """
    rows = conn.execute(
        """
        SELECT l.src_ref_id, l.dst_ref_id
          FROM links l
         WHERE l.relation = 'cites'
           AND l.src_chunk_id IS NULL
           AND l.dst_chunk_id IS NULL
           AND l.meta ->> 'source' = %(src)s
           AND EXISTS (
                 SELECT 1 FROM chunks c
                  WHERE c.ref_id = l.src_ref_id AND c.ord >= 0
               )
           AND NOT EXISTS (
                 SELECT 1 FROM links l2
                  WHERE l2.relation = 'cites'
                    AND l2.src_ref_id = l.src_ref_id
                    AND l2.dst_ref_id = l.dst_ref_id
                    AND l2.src_chunk_id IS NOT NULL
               )
         ORDER BY l.src_ref_id, l.dst_ref_id
         LIMIT %(limit)s
           FOR UPDATE OF l SKIP LOCKED
        """,
        {"src": _LINK_SOURCE, "limit": limit},
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


# ── Per-paper sweep ────────────────────────────────────────────────


@dataclass
class _SweepResult:
    citers_found: int = 0
    links_created: int = 0
    resolved_now: int = 0
    #: Candidates over :func:`max_candidates_per_paper` — counted and
    #: skipped, never queued (todo 489406's per-paper breaker).
    capped: int = 0
    #: Set when the daily ceiling stopped this sweep mid-paper — the
    #: paper is left ``INBOUND:pending`` (unstamped) and resumes on the
    #: next UTC day, so the early return skips ``_mark_swept``/event.
    ceiling_hit: bool = False


def _fetch_paper_claim_info(
    conn: Connection, ref_id: int
) -> tuple[str, str, dict[str, Any]] | None:
    """``(title, claim_text, identifiers)`` for a paper — ``None`` if gone.

    ``claim_text`` is the title, optionally extended with the stored
    abstract (``meta.abstract``, the generic ingest field) when
    present — the best available stand-in for "the claim(s) Y makes",
    since inbound chase has no single ``finding``-scoped claim the way
    outbound chase does. Used only to lexically/LLM-locate the citing
    chunk and frame the verify prompt (see :func:`_resolve_citer_chunk`).
    """
    row = conn.execute(
        """
        SELECT r.title, r.meta,
               COALESCE(
                 (SELECT jsonb_object_agg(id_kind, id_value)
                    FROM ref_identifiers WHERE ref_id = r.ref_id),
                 '{}'::jsonb
               ) AS identifiers
          FROM refs r
         WHERE r.ref_id = %s AND r.retired_at IS NULL
        """,
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    title = str(row[0] or "")
    meta = dict(row[1] or {})
    abstract = str(meta.get("abstract") or "").strip()
    claim_text = f"{title}. {abstract[:500]}" if abstract else title
    return title, claim_text, dict(row[2] or {})


def _already_linked(conn: Connection, citer_ref_id: int, y_ref_id: int) -> bool:
    """True when this pass already recorded the paper-level ``cites``
    edge ``citer_ref_id → y_ref_id`` (``meta->>'source'`` filter, the
    same marker claim query 2 keys on).

    The resume-after-ceiling shortcut: a paper left ``pending`` mid-
    sweep re-iterates its full candidate list on the next pass, and
    done candidates must skip *before* spending daily budget — or a
    paper with more citers than the ceiling would re-spend every day
    on its own already-chased prefix and never finish."""
    return (
        conn.execute(
            """
            SELECT 1 FROM links
             WHERE src_ref_id = %s AND dst_ref_id = %s
               AND relation = 'cites'
               AND meta ->> 'source' = %s
             LIMIT 1
            """,
            (citer_ref_id, y_ref_id, _LINK_SOURCE),
        ).fetchone()
        is not None
    )


def _sweep_one_paper(
    conn: Connection,
    store: Store,
    y_ref_id: int,
    *,
    with_llm: bool,
    budget: _DailyBudget,
) -> _SweepResult:
    """Resolve up to ``max_candidates_per_paper()`` S2-known citers of
    ``y_ref_id``.

    Marks ``y_ref_id`` ``INBOUND:swept`` on every path out (S2 success,
    S2 failure with no identifiers to retry, or empty citer list) so
    the permanent "never re-sweep" contract holds even on a paper the
    sweep couldn't usefully process. A transient S2 failure (a real
    identifier exists but the call itself errored) is the one path
    left ``pending`` for retry next pass — joined by the daily-ceiling
    path: ``budget`` spent mid-sweep returns unstamped (``ceiling_hit``)
    and the paper re-claims on the next UTC day.
    """
    result = _SweepResult()
    info = _fetch_paper_claim_info(conn, y_ref_id)
    if info is None:
        # Ref vanished between claim and processing — nothing to mark.
        return result
    _title, _claim, identifiers = info

    graph = load_s2_citation_graph(identifiers)
    if graph is None:
        if _s2_paper_id(identifiers) is None:
            # No usable identifier at all — this will never resolve on
            # retry, so stop re-claiming it every pass.
            _mark_swept(conn, store, y_ref_id, reason="no_identifier")
        # else: transient S2 failure — leave pending, retry next pass.
        return result

    cited_by = graph.get("cited_by")
    citers = cited_by if isinstance(cited_by, list) else []
    result.citers_found = len(citers)

    cap = max_candidates_per_paper()
    if len(citers) > cap:
        # Best-first under the cap: the most-cited candidates win when S2
        # reports a citation_count (the narrow ``cited_by`` shape usually
        # lacks one — those entries keep S2's order, stable sort). The
        # skipped tail is counted, never queued.
        citers = sorted(
            citers,
            key=lambda c: c.get("citation_count") or 0,
            reverse=True,
        )[:cap]
        result.capped = result.citers_found - len(citers)

    for citing in citers:
        if budget.remaining <= 0:
            # Daily ceiling spent — stop cleanly, unstamped: the paper
            # stays INBOUND:pending and resumes after the UTC-date
            # rollover. Work already processed commits normally and is
            # skipped for free on the resume pass (``_already_linked``
            # below), so a paper with more citers than the ceiling
            # makes progress instead of re-spending on its own prefix.
            result.ceiling_hit = True
            return result
        citer_ref_id = _resolve_or_ingest_citer(conn, store, citing)
        if citer_ref_id is None or citer_ref_id == y_ref_id:
            continue
        if _already_linked(conn, citer_ref_id, y_ref_id):
            continue
        if not budget.take(conn):
            # A sibling host spent the last unit between the peek above
            # and this atomic bump — same clean stop.
            result.ceiling_hit = True
            return result
        # Link immediately, at whatever granularity is known — the
        # paper-level fact doesn't wait on chunk resolution.
        store.add_link(
            src_ref_id=citer_ref_id,
            dst_ref_id=y_ref_id,
            relation="cites",
            set_by="system",
            meta={"source": _LINK_SOURCE},
            conn=conn,
        )
        result.links_created += 1
        # If the citer already has chunks (a real corpus paper, or a
        # stub some earlier pass already landed), resolve the specific
        # citing chunk right now instead of waiting for the follow-up
        # claim query to pick it up on a later pass.
        if _fetch_chunks(conn, citer_ref_id):
            _resolve_citer_chunk(
                conn,
                store,
                citer_ref_id=citer_ref_id,
                cited_ref_id=y_ref_id,
                with_llm=with_llm,
            )
            result.resolved_now += 1

    _mark_swept(conn, store, y_ref_id, reason=None)
    store.append_event(
        y_ref_id,
        source=_SOURCE,
        event="swept",
        payload={
            "citers_found": result.citers_found,
            "links_created": result.links_created,
            "resolved_now": result.resolved_now,
            "capped": result.capped,
        },
        conn=conn,
    )
    return result


def _resolve_or_ingest_citer(
    conn: Connection, store: Store, citing: dict[str, Any]
) -> int | None:
    """Find-or-mint a corpus ref for one S2 ``cited_by`` entry.

    Mirrors ``chase._resolve_or_create_stub`` via the shared, more
    general ``Store.upsert_stub_paper`` (its own docstring: "Mirrors
    the chase worker's stub path") — auto-ingests an S2-known citer
    that isn't in corpus yet without blocking on it, exactly as the
    design doc asks. Returns ``None`` only when S2 gave us no usable
    identifier at all (can't dedup/fetch on a bare title).
    """
    identifiers: list[tuple[str, str]] = []
    doi = citing.get("doi")
    if doi:
        identifiers.append(("doi", str(doi)))
    s2_id = citing.get("s2_id")
    if s2_id:
        identifiers.append(("s2", str(s2_id)))
    if not identifiers:
        return None
    title = (citing.get("title") or "").strip() or None
    year = citing.get("year")
    year_int = int(year) if isinstance(year, int) else None
    # ``citing`` is one ``cited_by`` entry from ``ingest.citations._get_
    # citations`` — a narrower shape than the ``_normalize()``-produced
    # dict (title/doi/year/s2_id only, no abstract/fields/citation_count:
    # that endpoint is called with a deliberately narrow ``fields=``).
    # ``s2_stub_meta_if_present`` only builds (and stamps) the mint-time
    # S2 patch when ``citing`` actually carries something beyond that bare
    # shape — otherwise stamping ``s2_enriched_at`` here would permanently
    # skip stub_rank's real enrich pass on an empty patch. Lazy import
    # mirrors ``load_s2_citation_graph``'s own: this module is on the
    # always-on ``get(kind='paper')`` read path and must work in venvs
    # without the ``[paper]`` extra.
    from precis.ingest.semantic_scholar import s2_stub_meta_if_present

    s2_meta = s2_stub_meta_if_present(citing, now=datetime.now(UTC))
    ref_id, _created = store.upsert_stub_paper(
        identifiers=identifiers,
        title=title,
        year=year_int,
        authors=citing.get("authors"),
        set_by="system",
        s2_meta=s2_meta,
        conn=conn,
    )
    return int(ref_id)


def _resolve_citer_chunk(
    conn: Connection,
    store: Store,
    *,
    citer_ref_id: int,
    cited_ref_id: int,
    with_llm: bool,
) -> None:
    """Locate the citer's chunk that cites ``cited_ref_id`` + write the link.

    Reuses ``chase``'s own ``_locate_chunk_in_target`` (confirm/replace
    the lexical pick) and ``_verify_support_with_caveats`` (yes/partial/
    no + caveats) — the same hooks the outbound walk uses, per the
    design doc's explicit instruction not to reinvent them. Always
    writes a chunk-scoped ``cites`` link once the citer has any chunks
    at all (see module docstring for why: this is what makes claim
    query 2 self-terminating without a separate backoff).

    Second locate pass (``dst_pos``): once the citer's own chunk is
    located, that chunk's text is itself a query into ``cited_ref_id``'s
    chunks — "which paragraph of Y does this specific claim/quote
    actually engage with." Same lexical-overlap-then-LLM-confirm shape
    as the first pass (:func:`_locate_dst_chunk`), only attempted when
    ``with_llm`` (a pure-lexical guess with no LLM confirmation would
    too often mis-attribute the backlink to the wrong paragraph of Y,
    unlike the citer-side guess above, which is always written best-
    effort). A ``None`` result — no chunk of Y confidently matches — is
    a real, expected outcome (the citer engages with Y's paper-level
    contribution, not one specific passage), so ``dst_pos`` is simply
    left unset rather than forced to a guess.
    """
    chunks = _fetch_chunks(conn, citer_ref_id)
    if not chunks:  # pragma: no cover — callers already gate on this
        return
    info = _fetch_paper_claim_info(conn, cited_ref_id)
    claim_text = info[1] if info is not None else ""
    citer_info = _fetch_paper_claim_info(conn, citer_ref_id)
    citer_cite_key = citer_info[0] if citer_info is not None else f"ref:{citer_ref_id}"

    claim_tokens = _tokenize(claim_text)
    if claim_tokens:
        proposed = max(chunks, key=lambda c: _overlap(claim_tokens, _tokenize(c[2])))
    else:
        proposed = chunks[0]

    located = proposed
    if with_llm:
        confirmed = _locate_chunk_in_target(
            claim=claim_text,
            proposed=proposed,
            alternates=[c for c in chunks if c[0] != proposed[0]][:3],
        )
        # ``None`` = "no shown chunk supports it" per the LLM's own
        # judgment. Still write the lexical-best guess (with no verdict
        # attached) rather than leaving the pair unresolved forever —
        # see module docstring's self-terminating-claim-query rationale.
        located = confirmed if confirmed is not None else proposed

    # ``store.add_link``'s ``src_pos``/``dst_pos`` take the chunk *ord*
    # (a per-ref position), not the raw ``chunk_id`` — the chunk_id in
    # ``located`` is only needed by the LLM hooks above, which take the
    # whole tuple.
    _chunk_id, chunk_ord, chunk_text = located
    link_meta: dict[str, Any] = {"source": _LINK_SOURCE}
    if with_llm:
        verification = _verify_support_with_caveats(
            claim=claim_text,
            scope={},
            target_cite_key=citer_cite_key,
            target_chunk_ord=chunk_ord,
            target_chunk_text=chunk_text,
        )
        if verification:
            link_meta["supports"] = verification.get("supports")
            caveats = verification.get("caveats") or []
            if caveats:
                link_meta["caveats"] = caveats

    dst_ord: int | None = None
    if with_llm:
        y_located = _locate_dst_chunk(conn, cited_ref_id, chunk_text)
        if y_located is not None:
            dst_ord = y_located[1]

    store.add_link(
        src_ref_id=citer_ref_id,
        dst_ref_id=cited_ref_id,
        src_pos=chunk_ord,
        dst_pos=dst_ord,
        relation="cites",
        set_by="system",
        meta=link_meta,
        conn=conn,
    )


def _locate_dst_chunk(
    conn: Connection, cited_ref_id: int, citer_chunk_text: str
) -> tuple[int, int, str] | None:
    """Second locate pass: which chunk of ``cited_ref_id`` (Y) the
    citer's own located chunk is actually about.

    Mirrors the citer-side lexical proposal (:func:`_tokenize`/
    :func:`_overlap`) against Y's own body chunks, then hands the
    citer's chunk text to the same LLM confirm hook
    (``_locate_chunk_in_target``) used for the first pass — asking it
    to accept the lexical pick or choose a better alternate from Y's
    chunks. Returns ``None`` (no chunk of Y confidently matches, or Y
    has no body chunks yet) rather than forcing a guess — callers must
    leave ``dst_pos`` unset in that case.
    """
    y_chunks = _fetch_chunks(conn, cited_ref_id)
    if not y_chunks:
        return None
    query_text = citer_chunk_text[:1500]
    query_tokens = _tokenize(query_text)
    if query_tokens:
        y_proposed = max(
            y_chunks, key=lambda c: _overlap(query_tokens, _tokenize(c[2]))
        )
    else:
        y_proposed = y_chunks[0]
    return _locate_chunk_in_target(
        claim=query_text,
        proposed=y_proposed,
        alternates=[c for c in y_chunks if c[0] != y_proposed[0]][:3],
    )


def _mark_swept(
    conn: Connection, store: Store, ref_id: int, *, reason: str | None
) -> None:
    store.add_tag(
        ref_id,
        Tag.closed(_INBOUND_NAMESPACE, _SWEPT),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    if reason:
        from psycopg.types.json import Jsonb

        conn.execute(
            "UPDATE refs SET meta = meta || %s, updated_at = now() WHERE ref_id = %s",
            (Jsonb({"inbound_swept_reason": reason}), ref_id),
        )


# ── Runner ─────────────────────────────────────────────────────────


def _log_pass_digest(
    budget: _DailyBudget, *, capped_per_paper: int, ceiling_hit: bool
) -> None:
    """One INFO digest row per working pass — the worker_logs surface
    (``precis logs --pass inbound_chase --payload``) the todo-489406
    flip review reads the caps from. The structured payload deliberately
    carries no ``claimed``/``ok``/``failed`` keys — those belong to the
    runner's own BatchResult row; duplicating them here would skew the
    ``activity_by_handler`` ok/fail recency filters."""
    remaining = max(0, budget.remaining)
    log.info(
        "inbound_chase: chased=%d capped_per_paper=%d ceiling_hit=%s "
        "remaining_today=%d",
        budget.chased,
        capped_per_paper,
        ceiling_hit,
        remaining,
        extra={
            "payload": {
                "handler": "inbound_chase",
                "chased": budget.chased,
                "capped_per_paper": capped_per_paper,
                "ceiling_hit": ceiling_hit,
                "remaining_today": remaining,
            }
        },
    )


def run_inbound_chase_pass(
    store: Store,
    *,
    limit: int = 8,
    with_llm: bool = True,
    now: datetime | None = None,
) -> dict[str, int]:
    """One pass: sweep newly-activated papers + resolve any citer stubs
    that have landed chunks since an earlier sweep.

    ``limit`` bounds *each* claim query separately (sweeps are the
    expensive half — one S2 call plus N stub-mints/LLM-verifies each —
    so a small default keeps a single pass bounded even though the
    sweep itself is capped per paper). ``with_llm`` defaults True:
    this whole pass is dark behind ``PRECIS_INBOUND_CHASE_ENABLED``, so
    reaching here already means the LLM-cost decision was made (see
    module docstring) — independent of ``PRECIS_CHASE_LLM``. ``now``
    pins the UTC day the daily counter keys on (tests inject it to
    drive the rollover; prod passes nothing).

    Returns the standard ``{claimed, ok, failed}`` shape, plus
    ``report_only: True`` when the pass ended ceiling-blocked — either
    the daily counter was already spent at claim time, or it ran out
    mid-pass leaving claimed rows unprocessed. Those rows are reporting,
    not work: the runner's ``claimed>0 ⇒ any_work ⇒ no idle sleep``
    coupling would otherwise hot-poll a spent pass re-claiming the same
    pending papers until the UTC rollover (gr514421).
    """
    claimed = 0
    ok = 0
    failed = 0
    capped_per_paper = 0
    ceiling_hit = False

    day = now if now is not None else datetime.now(UTC)
    budget = _DailyBudget(ceiling=daily_limit(), remaining=0, key=_daily_key(day))
    with store.pool.connection() as conn:
        budget.remaining = budget.ceiling - _daily_used(conn, budget.key)
        conn.commit()

    if budget.remaining <= 0:
        # Daily ceiling already spent — end cleanly before claiming:
        # nothing stamped, nothing locked; the pending queue resumes
        # after the UTC-date rollover.
        _log_pass_digest(budget, capped_per_paper=0, ceiling_hit=True)
        return {"claimed": 0, "ok": 0, "failed": 0, "report_only": True}

    with store.pool.connection() as conn:
        pending = _claim_pending_papers(conn, limit=limit)
        follow_ups = _claim_citers_needing_chunk_resolution(conn, limit=limit)
        conn.commit()

    claimed += len(pending) + len(follow_ups)

    for y_ref_id in pending:
        if budget.remaining <= 0:
            ceiling_hit = True
            break
        try:
            with store.pool.connection() as conn:
                res = _sweep_one_paper(
                    conn, store, y_ref_id, with_llm=with_llm, budget=budget
                )
                conn.commit()
            capped_per_paper += res.capped
            ceiling_hit = ceiling_hit or res.ceiling_hit
            ok += 1
        except Exception:  # pragma: no cover — defensive, mirrors chase.py
            log.warning(
                "inbound_chase: sweep failed for paper #%d", y_ref_id, exc_info=True
            )
            failed += 1

    for citer_ref_id, cited_ref_id in follow_ups:
        try:
            with store.pool.connection() as conn:
                if not budget.take(conn):
                    # Same clean-stop contract as the sweep arm: the
                    # pair stays resolvable and re-claims tomorrow.
                    ceiling_hit = True
                    conn.commit()
                    break
                _resolve_citer_chunk(
                    conn,
                    store,
                    citer_ref_id=citer_ref_id,
                    cited_ref_id=cited_ref_id,
                    with_llm=with_llm,
                )
                conn.commit()
            ok += 1
        except Exception:  # pragma: no cover — defensive
            log.warning(
                "inbound_chase: chunk resolution failed for citer #%d -> #%d",
                citer_ref_id,
                cited_ref_id,
                exc_info=True,
            )
            failed += 1

    if claimed or ceiling_hit:
        _log_pass_digest(
            budget, capped_per_paper=capped_per_paper, ceiling_hit=ceiling_hit
        )

    result: dict[str, int] = {"claimed": claimed, "ok": ok, "failed": failed}
    if ceiling_hit:
        # gr514421: the ceiling stopped this pass with claimed rows left
        # unprocessed — report the locked count for observability but
        # flag it so the run loop does not treat it as work and skip its
        # idle sleep (the same rows would be re-claimed immediately).
        result["report_only"] = True
    return result


__all__ = [
    "inbound_chase_enabled",
    "mark_paper_active",
    "run_inbound_chase_pass",
]
