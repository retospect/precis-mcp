"""hub_refine — periodic, converging enrichment of existing taproot claim hubs.

Stage 5 (*widen*) of the claim lifecycle in ``precis.taproot``'s package
docstring; operational runbook ``docs/runbooks/taproot-chase-enablement.md``.
Closes a gap the shipped taproot phases leave open: evidence attaches to a claim hub only as
a side effect of chasing a *finding* (the forward bridge,
``workers/chase.py``'s ``_taproot_bridge``) or when a human hands
``precis taproot mint`` a supporter list. Neither ever looks at an
**existing** hub and asks "what else in the corpus supports this claim?" —
so a hub minted off a single draft cite sits at one corroborator forever,
even when the primary source already sits un-attached in the same corpus.

This pass does exactly that, per hub, claimed off a **due-set** rather than
a blind periodic rescan (the incremental-trigger design,
``workers/chase_trigger.py``):

1. **Claim** — ``TAPROOT:claim`` / ``STATUS:canonical`` findings due for
   refine (see :func:`_claim_hubs_due_for_refine` for the full predicate: a
   ``TAPROOT_DUE`` tag from the trigger pass, never-refined, an edited
   claim reopening it, or the long backstop), never-refined first, oldest
   ``last_refined_at`` next, ``SKIP LOCKED``, ``LIMIT`` :func:`_hubs_per_pass`
   — mirrors ``workers/inbound_chase.py``'s claim-query shape. A **compound**
   claim hub (docs/backlog/taproot-atomic-claims.md — one carrying a live
   inbound ``conjunct-of`` edge from a live finding, i.e. it decomposed into
   atomic conjunct hubs) is excluded from this due-set entirely: its only
   possible write here is a direct evidence attach, which
   ``taproot.hub.attach_evidence`` now refuses (raises ``BadInput``) —
   claiming it would just raise every pass, roll back the per-hub tx, and
   grind the attempt-lease/backstop machinery forever without ever
   converging. :func:`_is_compound_hub` mirrors this same predicate as a
   defensive per-hub check inside :func:`_refine_one_hub`, for the race
   window between claim and processing (see that function's docstring).
   The predicate is deliberately re-derived here rather than imported —
   same three-deliberate-copies precedent as ``seniority._is_claim_hub``
   mirroring ``hub._is_claim_hub`` (docs/backlog/taproot-atomic-claims.md
   "cross-task seam").
2. **Discover** — TWO sources merged into one per-hub candidate list
   (the shipped citation-taproot-resolve proposal, git history), citation
   candidates first
   so they win the shared per-source dedup slot:
   (a) a semantic (embedding-ANN) search over paper **and patent** body
       chunks for the claim sentence, top-``PRECIS_TAPROOT_REFINE_TOPK``
       (docs/backlog/patent-evidence-parity.md). Note: *not*
       ``taproot.canon.block`` — that ANN is over hub *cards* (dedup against
       other hubs); this needs paper/patent-*chunk* neighbours, the same
       ``store.search_blocks`` engine ``PaperHandler.search`` drives (see
       ``handlers/_paper_search.py``), run in ``mode='semantic'`` so
       ``PRECIS_TAPROOT_REFINE_MIN_SIM`` (an optional cosine-distance floor)
       means what its name says. The paper leg and the patent leg are two
       separate ``store.search_blocks`` calls (the mode-dispatched wrapper
       takes one ``kind=`` string, not a list) merged by score and
       truncated back to ``topk`` — the per-hub bounded-spend guarantee
       (below) doesn't grow just because a second kind feeds discovery.
       **Grounding policy**: a patent's *claims*-section blocks
       (``chunks.meta['patent_block'] == 'claim'``,
       ``handlers/_patent_claims.py``) are legal scope, not empirical
       support, and are dropped before they can ever become a candidate —
       only description/abstract blocks are eligible. Citation-following
       (below) stays paper-only; patent citation graphs aren't parsed.
   (b) **citation-following** (:func:`_citation_candidates`): the hub's own
       evidence grounding chunks → ``chunk_citations`` →
       ``taproot.resolve_citation`` → the *held* cited paper → a
       paper-scoped semantic search for its top passage. So a claim reading
       "X is true [34]" is checked against what [34] actually *is*.
3. **Filter** — drop a candidate source ref (paper or patent) already
   carrying a ``corroborates`` edge on this hub (the idempotency precheck,
   done *before* any LLM spend) or already recorded in the hub's rejection
   memo (``meta['taproot_rejected']`` — a ``supports=no`` verdict from an
   earlier pass, judged once, never re-verified).
4. **Verify** — ``workers._chase_llm._verify_support_with_caveats`` per
   surviving candidate. When the candidate source is a patent, the prompt
   picks up patent-aware reading rules (background/prior-art recitations
   attribute knowledge to *others*, not the patentee; a worked example may
   be prophetic — US present-tense convention — which is a caveat, not a
   disqualifier).
5. **Write** — a ``yes``, or a ``partial`` whose caveats *scope* the
   support rather than negate it (``contradicts=false``) → an evidence
   edge via ``taproot.hub.attach_evidence`` (role ``corroborates``, meta
   carrying ``support``/``caveats``/``source_handle``). A ``no`` — or a
   ``partial`` flagged ``contradicts`` (the chunk runs counter to the
   claim, or is merely on-topic without substantiating it) → append to
   the rejection memo. Either way the candidate is judged once; the
   ``contradicts`` gate keeps on-topic-but-non-supporting papers out of
   the living cite without paying to re-verify them each pass.
6. **Stamp** — ``meta.last_refined_at`` and ``meta.last_refined_sha`` (the
   claim sentence's :func:`taproot.canon.claim_sha` at refine time) are set
   unconditionally (even an empty pass with zero new candidates), so the
   claim query's ``never-refined`` / ``never-reopened`` conditions hold and
   the hub naturally drains out of the due-set until something re-marks it
   (a new near paper, a title edit, or the backstop). A **sha-reopen** — the
   stored ``last_refined_sha`` no longer matches the live title — clears the
   rejection memo *before* discovery: the claim itself changed, so an old
   ``supports=no`` verdict on the previous wording may no longer hold.

**Reopen gate vs. decomposition.** A compound hub never reaches step 6 (it's
excluded upstream, see step 1), so a human rewording a compound hub's title
costs nothing here and — critically — does NOT re-run decomposition: atoms
vs. compound is decided once, at extraction time
(``taproot.canon.extract_claim``), never inside this pass. A reworded
compound just sits with a stale set of ``conjunct-of`` atoms until a
separate, human-run migration pass revisits it (docs/backlog/
taproot-atomic-claims.md's sequencing note) — this pass has no opinion on
whether a compound's conjuncts still match its (possibly now-different)
wording. Per-atom rewording is unaffected: an atom is an ordinary hub, so
its own sha-reopen (item 3 above / :func:`_is_hub_due`) still clears its own
rejection memo at the correct (atom) grain.

A raise anywhere in steps 2-5 (a per-candidate verify-LLM failure, a DB
error in ``attach_evidence``, ...) means step 6 never runs and the whole
per-hub transaction rolls back — so, absent a separate signal, the SAME due
reason (most often "never refined") would hold forever and the hub
re-verifies every candidate against the LLM again next sweep, unbounded
(OPEN-ITEMS "Unbraked LLM-pass cluster"). :func:`_claim_hubs_due_for_refine`
closes this: it writes a claim-time ``TAPROOT_REFINE_ATTEMPT`` lease
(:data:`_ATTEMPT_NS`, TTL'd via ``ref_tags.expires_at``) for every locked
hub in the SAME already-committed transaction as the ``TAPROOT_DUE`` pop —
so the lease survives a later raise and brakes the hub from re-claim for
:data:`ATTEMPT_COOLDOWN_MIN`. Step 6 clears the lease again on every
completed run (success or a clean no-op), so a genuine re-trigger is never
blocked by a stale lease left over from an earlier finished pass.

Never a periodic full re-scan: idempotent attach + pre-verify existence
check + rejection memo + due-set claim query together bound the per-run LLM
spend to (at most) ``HUBS_PER_PASS x (TOPK + grounded-cite count)`` calls —
adding the patent leg to the semantic source doesn't grow this bound: the two
kind-scoped legs are merged by score and truncated back to ``topk`` before
Filter→Verify ever runs. The citation source adds at most one scoped verify
per held paper the hub *already* grounds a citation against (a small, bounded
set), and the shared per-source dedup means a source reached by both
discover sources is still verified only once — in practice far less once
memos fill in. See the build ticket's "Non-negotiable: it must converge" for
the full rationale.

Ship dark: this is a **service**, so the switch is a ``service_config`` prio
row — ``precis service prio <host> hub_refine 1``, live, no redeploy. There is
no env flag: since the §L cutover a ``ServiceSpec``'s ``enable_env`` is never
read (``cli/worker.py::_should_register``), so setting
``PRECIS_TAPROOT_REFINE_ENABLED`` does nothing. Enable on exactly **one host**
— the rejection memo is a read-modify-write on ``meta``
(``docs/runbooks/taproot-chase-enablement.md``).

Once claiming work, the pass always verifies with the
LLM — there is no separate ``with_llm`` toggle here (unlike ``chase``): a
hub-refine run that can't verify can't do anything, so reaching this pass
at all already implies paying for it. The one hard dependency is the
embedder (discovery needs a query vector); if none is wired the pass logs
a warning and no-ops for the whole cycle (mirrors the forward bridge's own
embedder-unavailable degrade, ``workers/chase.py``'s ``_taproot_bridge``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.store.types import Tag
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    claim_hub_predicate_sql,
    claim_sha,
)
from precis.taproot.hub import HUB_ROLES, attach_evidence, run_retraction_checks
from precis.taproot.resolve import resolve_citation
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.workers._chase_llm import _verify_support_with_caveats, is_corroborating

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The claim-hub tag predicate, pre-rendered for embedding into this
#: module's own ``WHERE`` clauses (:func:`~precis.taproot.canon.claim_hub_predicate_sql`
#: — the single source of truth for "is this ref a claim hub", not a
#: fourth reinvention of the pair of ``EXISTS`` clauses).
_CLAIM_HUB_SQL = claim_hub_predicate_sql()

#: The evidence-edge role hub-refine always attaches with — never
#: ``establishes`` (originator promotion is derived elsewhere, see the
#: module docstring's "Originators are still derived" note in the build
#: ticket).
_ROLE = "corroborates"

#: ``finding.meta`` keys this pass reads/writes.
_META_LAST_REFINED_AT = "last_refined_at"
_META_LAST_REFINED_SHA = "last_refined_sha"
_META_REJECTED = "taproot_rejected"
#: Citation-following (citation-taproot-resolve, shipped — git history): a
#: ``supports=no`` verdict against a paper reached by *following a claim's
#: own inline citation* is recorded here as ``{marker, cited_ref,
#: from_chunk}`` — the queryable "we read the cited paper and the claimed
#: content isn't there" red-flag record (rendered red on the claim page).
_META_CITATION_MISSES = "citation_misses"
#: Resolved-but-not-held cited papers (``{doi, marker, from_chunk}``) —
#: surfaced for display ("cites a paper we don't hold"), never fetched here
#: (auto-fetch is out of scope, a later proposal).
_META_UNRESOLVED = "unresolved_citations"

#: The trigger pass's due-marker (``workers/chase_trigger.py``) — a closed
#: ref tag on a claim hub meaning "a new near paper landed, refresh me".
#: Popped here at claim time (see :func:`_claim_hubs_due_for_refine`).
_DUE_NS = "TAPROOT_DUE"
_DUE_VALUE = "1"

#: Claim-time attempt lease (OPEN-ITEMS "Unbraked LLM-pass cluster"): a
#: ``TAPROOT_DUE``/``never-refined``/sha-reopen/backstop due-hub whose
#: discover+verify loop raises never reaches ``_refine_one_hub``'s own
#: unconditional final ``last_refined_at`` stamp — the whole per-hub
#: transaction rolls back, so (unlike a completed pass) NOTHING records
#: that this hub was even attempted. Without a separate signal the same
#: due reason (most commonly "never refined") holds forever, and the hub
#: re-verifies every candidate against the LLM again next sweep,
#: unbounded. Fix: a ``ref_tags`` TTL'd lease
#: (``expires_at`` — migration 0010), written for every locked hub in the
#: SAME already-committed transaction as the ``TAPROOT_DUE`` pop (so it
#: survives a later raise), and excluded from candidacy while unexpired.
#: Cleared again by :func:`_refine_one_hub` on every completed run (success
#: or a clean "nothing to do") so a genuine re-trigger (a fresh
#: ``TAPROOT_DUE`` tag, a sha-reopen) is never blocked by a stale lease
#: left over from an earlier completed pass — only an in-flight crash
#: leaves it standing, and only until it cools down.
_ATTEMPT_NS = "TAPROOT_REFINE_ATTEMPT"
_ATTEMPT_VALUE = "1"
ATTEMPT_COOLDOWN_MIN = 30


def _backstop_hours() -> float:
    """``PRECIS_TAPROOT_REFINE_BACKSTOP_H`` — default **2160** (90d).

    Replaces the old ``PRECIS_TAPROOT_REFINE_INTERVAL_H`` weekly cadence
    now that the incremental trigger (``workers/chase_trigger.py``) marks a
    hub due promptly off a real corpus change: this is a LONG backstop so
    nothing is ever permanently stuck if a ``TAPROOT_DUE`` tag is lost to a
    failed pass, not a scheduling cadence.
    """
    try:
        return float(os.environ.get("PRECIS_TAPROOT_REFINE_BACKSTOP_H", "2160"))
    except ValueError:
        return 2160.0


def _hubs_per_pass() -> int:
    try:
        return int(os.environ.get("PRECIS_TAPROOT_REFINE_HUBS_PER_PASS", "8"))
    except ValueError:
        return 8


def _topk_default() -> int:
    try:
        return int(os.environ.get("PRECIS_TAPROOT_REFINE_TOPK", "8"))
    except ValueError:
        return 8


def _min_sim_default() -> float | None:
    """Optional cosine-distance floor — unset by default (no floor)."""
    raw = os.environ.get("PRECIS_TAPROOT_REFINE_MIN_SIM")
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ── claim query ────────────────────────────────────────────────────


def _is_hub_due(
    *,
    is_due_tagged: bool,
    last_refined_at: str | None,
    last_refined_sha: str | None,
    title: str,
    backstop_h: float,
) -> bool:
    """The due predicate — any of:

    1. carries a ``TAPROOT_DUE`` tag (the trigger pass marked a new near
       paper), or
    2. never refined (``last_refined_at`` absent), or
    3. edited since last refine (stored ``last_refined_sha`` absent or no
       longer matches the live title's :func:`taproot.canon.claim_sha` —
       a reopen), or
    4. the long backstop has elapsed (nothing is ever permanently stuck if
       a due-tag is lost to a failed pass).
    """
    if is_due_tagged:
        return True
    if last_refined_at is None:
        return True
    if last_refined_sha is None or last_refined_sha != claim_sha(title):
        return True
    try:
        refined_at = datetime.fromisoformat(last_refined_at)
    except (TypeError, ValueError):
        # Malformed timestamp -- fail open (due) rather than get stuck.
        return True
    if refined_at.tzinfo is None:
        refined_at = refined_at.replace(tzinfo=UTC)
    return refined_at < datetime.now(UTC) - timedelta(hours=backstop_h)


def _claim_hubs_due_for_refine(
    conn: Connection, store: Store, *, limit: int, backstop_h: float
) -> list[int]:
    """Lock and return up to ``limit`` claim-hub ``ref_id``s due for refine.

    Due-set claim query (see :func:`_is_hub_due`), never-refined first,
    oldest ``last_refined_at`` next, ``SKIP LOCKED``. The sha-reopen check
    isn't SQL-computable (:func:`taproot.canon.claim_sha` is Python-side
    blake2b), so this scans the whole (small, ~1.2k) canonical claim-hub
    set, computes due-ness in Python, then re-locks just the winning
    ``limit`` ids with a second ``FOR UPDATE SKIP LOCKED`` — a concurrent
    pass already holding one of those rows drops it from the returned set,
    same concurrency-safety as the old single-query form.

    Pops each claimed hub's ``TAPROOT_DUE`` tag in this same call (the
    work-queue pop) — if a new chunk re-marks the hub mid-processing, it
    simply re-triggers next pass. Also writes each locked hub's
    :data:`_ATTEMPT_NS` claim-time lease here, in the same commit — see
    that constant's docstring for why.

    Excludes **compound** claim hubs (docs/backlog/taproot-atomic-claims.md):
    a ``NOT EXISTS`` over an inbound live ``conjunct-of`` edge from a live
    ``finding`` — the same predicate :func:`_is_compound_hub` checks per-hub,
    deliberately re-derived here rather than shared (see the module
    docstring's "cross-task seam" note). A compound hub's only possible
    write is a direct evidence attach, which ``taproot.hub.attach_evidence``
    now refuses — claiming one here would just raise every pass and never
    converge.
    """
    rows = conn.execute(
        f"""
        SELECT r.ref_id, r.title, r.meta,
               EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(due_ns)s
                    AND t.value = %(due_value)s
               ) AS is_due_tagged,
               EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(attempt_ns)s
                    AND (rt.expires_at IS NULL OR rt.expires_at > now())
               ) AS has_attempt_lease
          FROM refs r
         WHERE r.kind = 'finding'
           AND r.deleted_at IS NULL
           AND {_CLAIM_HUB_SQL}
           AND NOT EXISTS (
                 SELECT 1 FROM links l
                  JOIN refs a ON a.ref_id = l.src_ref_id
                 WHERE l.dst_ref_id = r.ref_id
                   AND l.relation = 'conjunct-of'
                   AND a.kind = 'finding'
                   AND a.deleted_at IS NULL
               )
        """,
        {
            "due_ns": _DUE_NS,
            "due_value": _DUE_VALUE,
            "attempt_ns": _ATTEMPT_NS,
            **CLAIM_HUB_PREDICATE_PARAMS,
        },
    ).fetchall()

    candidates: list[tuple[int, str | None]] = []
    for ref_id, title, meta, is_due_tagged, has_attempt_lease in rows:
        if has_attempt_lease:
            # A prior attempt raised mid-loop and left its lease standing —
            # brake this hub from re-claim until it cools down, regardless
            # of which due reason would otherwise fire.
            continue
        meta = dict(meta or {})
        last_refined_at = meta.get(_META_LAST_REFINED_AT)
        last_refined_sha = meta.get(_META_LAST_REFINED_SHA)
        if _is_hub_due(
            is_due_tagged=bool(is_due_tagged),
            last_refined_at=last_refined_at,
            last_refined_sha=last_refined_sha,
            title=str(title or ""),
            backstop_h=backstop_h,
        ):
            candidates.append((int(ref_id), last_refined_at))

    # never-refined first, then oldest last_refined_at, then ref_id for a
    # deterministic tie-break -- mirrors the old ORDER BY ... NULLS FIRST.
    candidates.sort(key=lambda item: (item[1] is not None, item[1] or "", item[0]))
    candidate_ids = [ref_id for ref_id, _ in candidates[:limit]]
    if not candidate_ids:
        return []

    locked_rows = conn.execute(
        "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) FOR UPDATE SKIP LOCKED",
        (candidate_ids,),
    ).fetchall()
    priority = {ref_id: i for i, ref_id in enumerate(candidate_ids)}
    locked_ids = sorted((int(r[0]) for r in locked_rows), key=lambda rid: priority[rid])

    for ref_id in locked_ids:
        store.add_tag(
            ref_id,
            Tag.closed(_ATTEMPT_NS, _ATTEMPT_VALUE),
            set_by="system",
            expires_at=datetime.now(UTC) + timedelta(minutes=ATTEMPT_COOLDOWN_MIN),
            conn=conn,
        )
        store.remove_tag(ref_id, Tag.closed(_DUE_NS, _DUE_VALUE), conn=conn)

    return locked_ids


def _dedup_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order-preserving dedup of small JSON-able record dicts (citation
    misses / unresolved cites) — the accumulation across passes appends,
    but a re-derivation of the same ``{marker, cited_ref, from_chunk}`` must
    not grow the list every pass (convergence)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rec in records:
        key = json.dumps(rec, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _is_compound_hub(conn: Connection, ref_id: int) -> bool:
    """True iff ``ref_id`` carries a live inbound ``conjunct-of`` edge from a
    live ``finding`` (docs/backlog/taproot-atomic-claims.md) — i.e. it is a
    compound claim hub with atomic conjuncts linked to it, rather than an
    atom or a plain (undecomposed) claim hub.

    Deliberately re-derives the same predicate :func:`_claim_hubs_due_for_refine`
    already applies as a ``NOT EXISTS`` filter, rather than sharing a
    connection-agnostic helper — the "cross-task seam" precedent
    (``seniority._is_claim_hub`` mirrors ``hub._is_claim_hub``; each module
    opens its own connection and keeps its own copy). Used only as a
    defensive per-hub check inside :func:`_refine_one_hub`, for the narrow
    race window between :func:`_claim_hubs_due_for_refine`'s claim and this
    function's processing — the due-set query above already keeps compounds
    from being claimed in the first place.
    """
    row = conn.execute(
        """
        SELECT 1
          FROM links l
          JOIN refs a ON a.ref_id = l.src_ref_id
         WHERE l.dst_ref_id = %s
           AND l.relation = 'conjunct-of'
           AND a.kind = 'finding'
           AND a.deleted_at IS NULL
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return row is not None


def _fetch_hub_info(conn: Connection, ref_id: int) -> tuple[str, dict[str, Any]] | None:
    """``(title, meta)`` for a live hub finding — ``None`` if it's gone."""
    row = conn.execute(
        "SELECT title, meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0] or ""), dict(row[1] or {})


# ── per-hub refine ─────────────────────────────────────────────────


def _attached_source_ids(conn: Connection, hub_ref_id: int) -> set[int]:
    """Source ref_ids (paper *or* patent) already carrying an evidence edge
    (any ``HUB_ROLES`` role) on this hub.

    Hub-refine dedups at the **source-ref** level — a supporter is a paper or
    a patent, not a passage — so a source already attached via *any*
    grounding chunk is skipped before verify. This is load-bearing under the
    chunk-scoped edge model (evidence edges now carry ``src_chunk_id``): a
    per-chunk ``_evidence_edge_exists`` check would miss the same source
    re-surfaced via a *different* chunk on a later pass and re-verify +
    re-attach it every run — breaking convergence. One query per hub also
    collapses the old per-candidate ``_evidence_edge_exists``
    connection-per-call N+1. ``taproot.hub._EVIDENCE_SRC_KINDS`` already
    admits both kinds through ``links``, so this query needs no kind filter
    of its own to cover patent evidence.
    """
    rows = conn.execute(
        "SELECT DISTINCT src_ref_id FROM links "
        "WHERE dst_ref_id = %s AND relation = ANY(%s)",
        (hub_ref_id, list(HUB_ROLES)),
    ).fetchall()
    return {int(r[0]) for r in rows}


@dataclass(frozen=True)
class _Candidate:
    """One discover-step candidate passage headed for Filter→Verify→Write.

    ``via`` distinguishes the two discover sources: ``"semantic"`` (the
    corpus-wide ANN — now over paper *and* patent body chunks, docs/
    proposals/patent-evidence-parity.md) and ``"citation"`` (a passage
    inside a paper the claim's own inline citation points at — patent
    citation graphs aren't parsed, so this source stays paper-only).
    ``marker`` / ``from_chunk`` are set only for ``"citation"`` — they
    carry the citation-miss provenance (which inline marker, in which
    grounding chunk) recorded when a citation-reached paper fails verify.
    ``ref`` may be a paper or a patent ref; ``source_ref_id`` names it
    accordingly rather than assuming paper.
    """

    block: Any
    ref: Any
    via: str
    marker: int | None = None
    from_chunk: int | None = None

    @property
    def source_ref_id(self) -> int:
        return int(self.ref.id)


def _citation_candidates(
    conn: Connection,
    store: Store,
    hub_ref_id: int,
    *,
    claim_sentence: str,
    query_vec: list[float],
) -> tuple[list[_Candidate], list[dict[str, Any]]]:
    """The **second** discover source (citation-taproot-resolve, shipped — git history): follow this hub's *own* evidence citations.

    For each grounding chunk of the hub's existing evidence edges
    (``links.src_chunk_id``), read ``chunk_citations`` → ``resolve_citation``
    → the *held* cited paper, then a paper-scoped semantic search
    (``scope_ref_id=<cited paper>``) with the claim sentence yields that
    paper's top candidate passage. Returns ``(candidates, unresolved)`` —
    ``unresolved`` collects resolved-but-not-held cites (``{doi, marker,
    from_chunk}``) for display.

    The scoped search deliberately applies **no** ``max_distance`` floor:
    citation-following's whole point is to verify against what the author
    actually cited even when that passage wouldn't clear the corpus-wide
    similarity floor — the floor governs only the semantic source.
    """
    rows = conn.execute(
        "SELECT DISTINCT src_chunk_id FROM links "
        "WHERE dst_ref_id = %s AND relation = ANY(%s) AND src_chunk_id IS NOT NULL",
        (hub_ref_id, list(HUB_ROLES)),
    ).fetchall()
    candidates: list[_Candidate] = []
    unresolved: list[dict[str, Any]] = []
    seen_cited: set[int] = set()
    for (src_chunk_id,) in rows:
        src_chunk_id = int(src_chunk_id)
        marker_rows = conn.execute(
            "SELECT marker FROM chunk_citations WHERE chunk_id = %s ORDER BY marker",
            (src_chunk_id,),
        ).fetchall()
        for (marker,) in marker_rows:
            marker = int(marker)
            res = resolve_citation(store, src_chunk_id, marker, conn=conn)
            if res is None:
                continue
            if res.held_ref_id is None:
                if res.doi:
                    unresolved.append(
                        {"doi": res.doi, "marker": marker, "from_chunk": src_chunk_id}
                    )
                continue
            cited = res.held_ref_id
            if cited == hub_ref_id or cited in seen_cited:
                continue
            seen_cited.add(cited)
            # Only the single top passage is used (citation-following verifies
            # one passage per cited paper), so rank just that — not topk.
            # No max_distance floor: we verify against what the author cited
            # even at low corpus-wide similarity (the verify step backstops).
            hits = store.blocks.search_blocks(
                q=claim_sentence,
                query_vec=query_vec,
                mode="semantic",
                kind="paper",
                scope_ref_id=cited,
                limit=1,
                max_distance=None,
            )
            if not hits:
                continue
            block, ref, _score = hits[0]
            candidates.append(
                _Candidate(
                    block=block,
                    ref=ref,
                    via="citation",
                    marker=marker,
                    from_chunk=src_chunk_id,
                )
            )
    return candidates, unresolved


#: Patent legal-claim blocks (``handlers/_patent_claims.py``'s
#: ``claim_block_meta`` marker) — grounding policy (docs/backlog/
#: patent-evidence-parity.md): legal scope is not empirical support, so
#: these never reach Verify. Description/abstract blocks carry no
#: ``patent_block`` value of ``"claim"`` (description blocks are tagged
#: ``"description"`` via ``DESCRIPTION_BLOCK_META``; abstract text isn't
#: currently chunked into a block at all), and a paper block never carries
#: this key either — so this filter is a no-op on every non-patent-claim
#: hit.
_PATENT_CLAIM_BLOCK = "claim"


def _drop_patent_claim_blocks(
    hits: list[tuple[Any, Any, float]],
) -> list[tuple[Any, Any, float]]:
    """Filter a ``store.search_blocks`` hit list, dropping patent
    legal-claim-section blocks so they never become grounding candidates."""
    return [
        (block, ref, score)
        for block, ref, score in hits
        if block.meta.get("patent_block") != _PATENT_CLAIM_BLOCK
    ]


def _refine_one_hub(
    conn: Connection,
    store: Store,
    hub_ref_id: int,
    *,
    embedder: Any,
    topk: int,
    min_sim: float | None,
    pending_checks: list[int] | None = None,
) -> None:
    """Discover + verify + attach corroborators for one hub, then stamp it.

    **Two discover sources, one Filter→Verify→Write tail** (docs/backlog/
    citation-taproot-resolve.md): the corpus-wide semantic ANN — now over
    paper *and* patent body chunks (docs/backlog/patent-evidence-
    parity.md) — and citation-following (:func:`_citation_
    candidates`, paper-only). Both merge into a single per-hub candidate
    list — **citation candidates first, so they win the slot** — deduped by
    source ref via ONE loop-local ``seen_sources`` set, so a source surfaced
    by both discover sources is verified exactly once. This keeps the
    module's bounded-spend guarantee (the citation source adds at most one
    scoped verify per held cited paper the hub already grounds against, so
    the per-hub worst case grows only by the hub's own grounded-citation
    count, not unboundedly; the patent leg merges into the same
    already-bounded ``topk`` semantic slot rather than adding one of its
    own — see :func:`_drop_patent_claim_blocks` for the grounding-policy
    exclusion of patent legal-claim blocks).

    Always writes ``meta.last_refined_at`` + ``meta.last_refined_sha`` on
    the way out (even when the hub's title is blank, or discovery/verify
    finds nothing new) — that unconditional stamp is what makes the claim
    query's due-set conditions (never-refined, sha-match) hold (see
    :func:`_claim_hubs_due_for_refine`).

    A **sha-reopen** — the stored ``last_refined_sha`` no longer matches
    the live title's :func:`taproot.canon.claim_sha` — clears the
    rejection memo (and the citation-miss / unresolved-cite records)
    *before* discovery/verify run: the claim wording changed, so an old
    ``supports=no`` verdict may no longer hold.

    **Defensive compound skip.** :func:`_claim_hubs_due_for_refine` already
    excludes compound hubs from the due-set, but a hub can *become*
    compound in the narrow window between that claim and this function
    running (a concurrent decomposition mints the ``conjunct-of`` edge).
    :func:`_is_compound_hub` re-checks here and, if true, drains the hub
    cleanly: it still stamps ``last_refined_at``/``last_refined_sha`` (this
    ref is live, unlike the vanished-ref case below) and clears the
    attempt lease, then returns before discovery/verify ever run — no
    evidence attach is attempted, so ``taproot.hub.attach_evidence``'s
    compound guard is never even reached.
    """
    info = _fetch_hub_info(conn, hub_ref_id)
    if info is None:
        # Ref vanished between claim and processing — nothing to stamp,
        # but this IS a completed run: clear the claim-time attempt lease
        # like the normal exit below does.
        store.remove_tag(hub_ref_id, Tag.closed(_ATTEMPT_NS, _ATTEMPT_VALUE), conn=conn)
        return
    title, meta = info
    if _is_compound_hub(conn, hub_ref_id):
        # Became compound between claim and processing — stamp-and-drain
        # (see the docstring above): no discovery/verify, no evidence
        # attach attempt.
        store.update_ref(
            hub_ref_id,
            meta_patch={
                _META_LAST_REFINED_AT: datetime.now(UTC).isoformat(),
                _META_LAST_REFINED_SHA: claim_sha(title),
            },
            conn=conn,
        )
        store.remove_tag(hub_ref_id, Tag.closed(_ATTEMPT_NS, _ATTEMPT_VALUE), conn=conn)
        return
    claim_sentence = title.strip()
    scope = dict(meta.get("scope") or {})
    new_sha = claim_sha(title)
    stored_sha = meta.get(_META_LAST_REFINED_SHA)
    reopened = stored_sha is not None and stored_sha != new_sha
    rejected: dict[str, Any] = {} if reopened else dict(meta.get(_META_REJECTED) or {})
    attached = _attached_source_ids(conn, hub_ref_id)

    # Citation-miss / unresolved-cite records accumulate across passes
    # (keyed by their record tuple, deduped at persist time). A sha-reopen
    # clears them like the rejection memo — the old verdicts may no longer
    # hold on the new wording.
    citation_misses: list[dict[str, Any]] = (
        [] if reopened else list(meta.get(_META_CITATION_MISSES) or [])
    )
    unresolved: list[dict[str, Any]] = (
        [] if reopened else list(meta.get(_META_UNRESOLVED) or [])
    )

    query_vec = embed_query(embedder, claim_sentence) if claim_sentence else None
    if claim_sentence and query_vec is None:
        log.warning(
            "hub_refine: embed returned no vector for hub #%d -- skipping "
            "discovery this pass rather than silently degrading to lexical",
            hub_ref_id,
        )
    if claim_sentence and query_vec is not None:
        # Discover source 1 (new): citation-following. Built first so its
        # passages win the shared-dedup slot over the semantic source.
        cite_cands, new_unresolved = _citation_candidates(
            conn,
            store,
            hub_ref_id,
            claim_sentence=claim_sentence,
            query_vec=query_vec,
        )
        unresolved.extend(new_unresolved)

        # Discover source 2 (existing, now two kind-scoped legs): corpus-wide
        # semantic ANN over paper chunks, plus a patent leg (docs/backlog/
        # patent-evidence-parity.md). ``store.search_blocks``'s mode-
        # dispatched wrapper takes one ``kind=`` string, not a list, so this
        # is two calls merged by score (ascending cosine distance) and
        # truncated back to ``topk`` -- the bounded-spend guarantee doesn't
        # grow with the number of kinds feeding discovery. Patent
        # legal-claim blocks are dropped before the merge: legal scope is
        # not empirical support (grounding policy).
        paper_hits = store.blocks.search_blocks(
            q=claim_sentence,
            query_vec=query_vec,
            mode="semantic",
            kind="paper",
            limit=topk,
            max_distance=min_sim,
        )
        patent_hits = _drop_patent_claim_blocks(
            store.blocks.search_blocks(
                q=claim_sentence,
                query_vec=query_vec,
                mode="semantic",
                kind="patent",
                limit=topk,
                max_distance=min_sim,
            )
        )
        sem_hits = sorted([*paper_hits, *patent_hits], key=lambda hit: hit[2])[:topk]
        sem_cands = [
            _Candidate(block=block, ref=ref, via="semantic")
            for block, ref, _score in sem_hits
        ]

        seen_sources: set[int] = set()
        for cand in [*cite_cands, *sem_cands]:
            source_ref_id = cand.source_ref_id
            if source_ref_id == hub_ref_id or source_ref_id in seen_sources:
                continue
            seen_sources.add(source_ref_id)
            # Precheck BEFORE verify (idempotency + rejection memo): a
            # source ref already an evidence supporter of this hub (any
            # role, any grounding chunk) or already judged ``no`` must never
            # cost another LLM call. Source-ref-level, not chunk-level —
            # see _attached_source_ids.
            if source_ref_id in attached or str(source_ref_id) in rejected:
                continue
            block, ref = cand.block, cand.ref
            verification = _verify_support_with_caveats(
                claim=claim_sentence,
                scope=scope,
                target_cite_key=ref.slug or f"ref:{source_ref_id}",
                target_chunk_ord=block.pos,
                target_chunk_text=block.text,
                source_kind=ref.kind,
            )
            if verification is None:
                # Transient LLM/dispatch failure — no verdict recorded,
                # so this candidate is simply retried next pass (neither
                # attached nor memoed as rejected).
                continue
            supports = verification.get("supports")
            contradicts = bool(verification.get("contradicts"))
            # Attach only genuine corroboration (shared gate
            # ``_chase_llm.is_corroborating``: a "yes", or a "partial" whose
            # caveats scope the support rather than negate it). A "partial"
            # flagged ``contradicts`` (the chunk runs counter to the claim)
            # is treated like a "no" — memoed as judged, NOT attached, so it
            # never dilutes the living cite and never costs a re-verify
            # (convergence: a non-attached verdict must land in the memo,
            # else it retries every pass forever).
            if is_corroborating(verification):
                attach_evidence(
                    store,
                    hub_ref_id=hub_ref_id,
                    paper_ref_id=source_ref_id,
                    role=_ROLE,
                    meta={
                        "support": supports,
                        "caveats": list(verification.get("caveats") or []),
                        "source_handle": handle_registry.try_format(
                            ref.kind, block.id, chunk=True
                        ),
                    },
                    set_by="system",
                    conn=conn,
                    pending_checks=pending_checks,
                )
            elif supports in ("partial", "no"):
                # "no", or a contradicting "partial" — judged once, memoed.
                entry: dict[str, Any] = {
                    "at": datetime.now(UTC).isoformat(),
                    "supports": supports,
                    "contradicts": contradicts,
                }
                # A citation-reached rejection is ALSO a citation miss: "we
                # read the paper the claim cites and the content isn't there"
                # — marked on the memo + recorded as a queryable red flag.
                # DELIBERATE: this fires for BOTH a plain ``no`` AND a
                # contradicting ``partial`` (the ``elif`` above), slightly
                # beyond the AC's literal "supports=no" — a cited source that
                # *contradicts* the claim is a stronger miss than a bare "no"
                # and belongs in the same red-flag bucket, not a separate one.
                if cand.via == "citation":
                    entry["via"] = "citation"
                    citation_misses.append(
                        {
                            "marker": cand.marker,
                            "cited_ref": source_ref_id,
                            "from_chunk": cand.from_chunk,
                        }
                    )
                rejected[str(source_ref_id)] = entry
            else:
                # Verdict outside the {yes,partial,no} enum (missing key or
                # an LLM-schema regression) — neither attach nor memo, so it
                # retries next cadence; log so the regression isn't invisible.
                log.warning(
                    "hub_refine: hub #%d candidate source ref #%d got unexpected "
                    "verify verdict %r -- skipped (retries next cadence)",
                    hub_ref_id,
                    source_ref_id,
                    supports,
                )

    meta_patch: dict[str, Any] = {
        _META_LAST_REFINED_AT: datetime.now(UTC).isoformat(),
        _META_LAST_REFINED_SHA: new_sha,
    }
    if rejected or reopened:
        # Always persist on a reopen, even an emptied memo -- that's the
        # clear taking effect, not just skipped because nothing's pending.
        meta_patch[_META_REJECTED] = rejected
    misses_deduped = _dedup_records(citation_misses)
    if misses_deduped or reopened:
        meta_patch[_META_CITATION_MISSES] = misses_deduped
    unresolved_deduped = _dedup_records(unresolved)
    if unresolved_deduped or reopened:
        meta_patch[_META_UNRESOLVED] = unresolved_deduped
    store.update_ref(hub_ref_id, meta_patch=meta_patch, conn=conn)
    # This point is only reached on a completed run (a raise anywhere above
    # propagates out and this line never runs) -- clear the claim-time
    # attempt lease so a genuine re-trigger is never blocked by a stale one.
    store.remove_tag(hub_ref_id, Tag.closed(_ATTEMPT_NS, _ATTEMPT_VALUE), conn=conn)


# ── runner ─────────────────────────────────────────────────────────


def run_hub_refine_pass(
    store: Store,
    *,
    limit: int | None = None,
    embedder: Any | None = None,
    topk: int | None = None,
    min_sim: float | None = None,
) -> dict[str, int]:
    """One pass: claim due hubs, discover + verify + attach corroborators.

    Every keyword defaults to its ``PRECIS_TAPROOT_REFINE_*`` env knob
    when omitted (see the module-level ``_*_default``/``_*_hours``
    readers) — tests pass them explicitly to stay independent of the
    process environment. The due-set backstop (``PRECIS_TAPROOT_REFINE_
    BACKSTOP_H``, see :func:`_backstop_hours`) has no override kwarg here
    — it's a rarely-hit safety net, not a per-run tuning knob; tests that
    need to force it monkeypatch the env var.

    ``embedder=None`` degrades the whole pass to a logged no-op: no hubs
    are even claimed, since discovery has nothing to search with. This
    mirrors the forward bridge's (``workers/chase.py``) own
    embedder-unavailable degrade rather than silently falling through to
    ``store.search_blocks``'s internal lexical fallback, which would
    quietly turn "ANN over paper chunks" into a much weaker keyword
    match without ever telling the operator.

    Returns the standard ``{claimed, ok, failed}`` shape.
    """
    if embedder is None:
        log.warning("hub_refine: no embedder available -- pass degrades to a no-op")
        return {"claimed": 0, "ok": 0, "failed": 0}

    resolved_limit = limit if limit is not None else _hubs_per_pass()
    resolved_topk = topk if topk is not None else _topk_default()
    resolved_backstop_h = _backstop_hours()
    resolved_min_sim = min_sim if min_sim is not None else _min_sim_default()

    with store.pool.connection() as conn:
        hub_ids = _claim_hubs_due_for_refine(
            conn, store, limit=resolved_limit, backstop_h=resolved_backstop_h
        )
        conn.commit()

    claimed = len(hub_ids)
    ok = 0
    failed = 0

    for hub_ref_id in hub_ids:
        try:
            # Trigger-1 checks do Crossref HTTP and open their own
            # connections, so they are collected during the write and run
            # only after the commit below — never inside the transaction
            # (see ``taproot.hub.attach_evidence``).
            pending_checks: list[int] = []
            with store.pool.connection() as conn:
                _refine_one_hub(
                    conn,
                    store,
                    hub_ref_id,
                    embedder=embedder,
                    topk=resolved_topk,
                    min_sim=resolved_min_sim,
                    pending_checks=pending_checks,
                )
                conn.commit()
            run_retraction_checks(store, pending_checks, hub_ref_id=hub_ref_id)
            ok += 1
        except Exception:  # pragma: no cover — defensive, mirrors inbound_chase.py
            log.warning(
                "hub_refine: refine failed for hub #%d", hub_ref_id, exc_info=True
            )
            failed += 1

    return {"claimed": claimed, "ok": ok, "failed": failed}


__all__ = [
    "run_hub_refine_pass",
]
