"""hub_refine — periodic, converging enrichment of existing taproot claim hubs.

Stage 5 (*widen*) of the claim lifecycle (``precis.taproot`` package
docstring; runbook ``docs/runbooks/taproot-chase-enablement.md``). Closes
a gap the other taproot phases leave open: evidence attaches to a hub only
as a side effect of chasing a *finding* (the forward bridge,
``workers/chase.py``'s ``_taproot_bridge``) or a human's ``precis taproot
mint`` supporter list — nothing looks at an *existing* hub and asks "what
else in the corpus supports this claim?", so a hub minted off one draft
cite sits at one corroborator forever even with the primary source
sitting un-attached in the same corpus.

Claimed off a **due-set**, never a blind periodic rescan
(``workers/chase_trigger.py``'s incremental-trigger design):

1. **Claim** (:func:`_claim_hubs_due_for_refine`) — ``TAPROOT:claim``/
   ``STATUS:canonical`` findings due for refine (a ``TAPROOT_DUE`` tag,
   never-refined, an edit reopening it, or the long backstop),
   never-refined first then oldest ``last_refined_at``, ``SKIP LOCKED``,
   capped at :func:`_hubs_per_pass`. A **compound** claim hub (a live
   inbound ``conjunct-of`` edge — it decomposed into atoms,
   docs/backlog/taproot-atomic-claims.md) is excluded entirely: its only
   possible write is a direct evidence attach, which
   ``taproot.hub.attach_evidence`` refuses (``BadInput``), so claiming it
   would just raise every pass and never converge.
   :func:`_is_compound_hub` re-checks the same predicate defensively
   inside :func:`_refine_one_hub` for the claim/process race window.
2. **Discover** — two sources merged into one candidate list, citation
   candidates first (win the shared per-source dedup slot):
   (a) semantic ANN over paper **and patent** body chunks for the claim
   sentence, top-``PRECIS_TAPROOT_REFINE_TOPK`` — *not*
   ``taproot.canon.block`` (that's hub-card dedup); this is the
   ``store.search_chunks`` chunk-neighbour engine ``PaperHandler.search``
   uses, ``mode='semantic'`` so ``PRECIS_TAPROOT_REFINE_MIN_SIM`` applies.
   Paper and patent legs are separate calls merged by score and truncated
   back to ``topk`` (a second kind doesn't grow the spend bound). Patent
   *claims*-section blocks (legal scope, not empirical support) are
   dropped before candidacy; citation-following stays paper-only.
   (b) **citation-following** (:func:`_citation_candidates`) — the hub's
   grounding chunks → ``chunk_citations`` → ``taproot.resolve_citation`` →
   the held cited paper → a paper-scoped semantic search for its top
   passage, so "X is true [34]" gets checked against what [34] says.
3. **Filter** — drop a candidate already carrying a ``corroborates`` edge
   on this hub, or already in the rejection memo (``meta['taproot_rejected']``
   — a judged-once ``supports=no`` verdict, never re-verified), *before*
   any LLM spend. Both sets also feed ``exclude_ref_ids`` into the
   semantic legs, so a settled source can't occupy a ``topk`` slot (the
   precheck is authoritative — this is a budget guarantee, since the
   citation leg is unfiltered and verdicts land mid-loop).
4. **Verify** — ``workers._chase_llm._verify_support_with_caveats`` per
   surviving candidate; a patent source gets patent-aware reading rules
   (background/prior-art recitations attribute knowledge to *others*; a
   prophetic worked example is a caveat, not a disqualifier).
5. **Write** — ``yes``, or a ``partial`` that scopes rather than negates
   support (``contradicts=false``) → ``attach_evidence`` (role
   ``corroborates``, meta carries ``support``/``caveats``/``source_handle``).
   ``no``, or a ``contradicts``-flagged ``partial`` → append to the
   rejection memo. A ``contradicts`` verdict **also files a non-blocking
   ``disputes`` link** (:func:`_attach_disputes`, same call ADR 0073 makes
   on the reground path; docs/backlog/disputes-edge-nonblocking-
   disagreement.md D3) and queues the hub's **demotion**
   (:mod:`precis.nanopub.demote`,
   drained post-commit): a ``reviewed``/``signed`` hub reopens to
   ``candidate``; ``anchored``/``published`` raises for a human. Demotion
   is the only two-directional move here — everything else promotes.
6. **Stamp** — ``meta.last_refined_at``/``last_refined_sha``
   (:func:`taproot.canon.claim_sha` at refine time) set unconditionally,
   even on an empty pass, so the due-set conditions hold and the hub
   drains out until re-marked. A **sha-reopen** (stored sha ≠ live title)
   clears the rejection memo before discovery — the claim changed, so an
   old ``supports=no`` verdict may no longer hold.

Alongside discovery, every run also **re-verifies the hub's own
attached-but-unverified evidence** (:func:`_reverify_pinned_edges`) — an
edge minted with no verdict, or a mint-time default stamp, never
re-enters step 3's discovery precheck, so without this it stays
unverified forever. Corroborating verdicts get the full
``support``/``caveats``/``verified_by``/``verified_at``/``verified_claim_sha``
stamp (fingerprinted ``'hub-refine'``); non-corroborating ones are memoed
into ``meta.reground_seen`` (judged once per ``claim_sha``, additive-only),
capped at :data:`_REVERIFY_PER_PASS` calls/hub/pass.

**Reopen gate vs. decomposition.** A compound hub never reaches step 6
(excluded at step 1), so retitling it costs nothing here and does NOT
re-run decomposition — atoms-vs-compound is decided once, at extraction
(``taproot.canon.extract_claim``). A reworded compound just sits with a
stale ``conjunct-of`` set until a separate human-run migration revisits
it. An atom is an ordinary hub — its own sha-reopen still clears its own
memo at the atom grain.

A raise anywhere in steps 2-5 rolls back the whole per-hub transaction —
step 6 never runs, so absent a separate signal the hub would re-verify
every candidate against the LLM again next sweep, unbounded.
:func:`_claim_hubs_due_for_refine` closes this: it writes a claim-time
``TAPROOT_REFINE_ATTEMPT`` lease (:data:`_ATTEMPT_NS`, TTL'd) in the same
transaction as the ``TAPROOT_DUE`` pop, braking re-claim for
:data:`ATTEMPT_COOLDOWN_MIN` even across a raise. Step 6 clears the lease
on every completed run (success or clean no-op).

Never a periodic full re-scan: idempotent attach + precheck + rejection
memo + due-set claim together bound per-run spend to (at most)
``HUBS_PER_PASS x (TOPK + grounded-cite count + REVERIFY_PER_PASS)`` LLM
calls; the patent leg doesn't grow this (merged-then-truncated to
``topk``), and shared per-source dedup means a source reached by both
discover sources is verified once.

**Ship dark**: a service, gated by a ``service_config`` prio row
(``precis service prio <host> hub_refine 1``, live, no redeploy — no env
flag works post-§L). Enable on exactly **one host**: the rejection memo
is a read-modify-write on ``meta``.

**Reground** (``docs/backlog/taproot-reground.md``) grows this same pass
from additive-only enrichment into full re-grounding — deliberately not a
second worker; this is the one mechanism that improves an existing hub.
Active :class:`RegroundConfig` adds five things onto the same
claim→discover→verify→write→stamp path:

* **Fisheye + audit** (stages 1-2) — every current grounding chunk, with
  neighbours, goes to :func:`judge_edge_strict` — stricter than the
  minter's verifier, which yes's a proxy for uttering the claim. Primary
  content → KEEP; assert/defer/review-deferral/abstract-for-a-measurement/
  title/byline/cover/bibliography → PRUNE; primary-against → CONTRADICTS;
  default KEEP on uncertainty.
* **Convergence guard** — ``meta.reground_seen`` memoes each edge's
  verdict against ``claim_sha`` (re-judged at most once per wording,
  cleared by a sha-reopen alongside the rejection memo). Reground
  converges; it is not a periodic re-scan.
* **Deeper re-discovery** (stage 3) — the two discover sources at a
  deeper top-k, plus deeper passages inside already-grounding papers
  offered *first* (the primary move: pilot found 5/6 proxy prunes were
  same-paper depth corrections). :func:`claim_depth_policy` lets a
  definition/existence claim ground on an abstract but requires a body
  passage for a measurement/mechanism claim.
* **Prune, add-first, in code** — nothing removed inside the per-hub
  transaction. Prunes are planned (:class:`RegroundPlan`) and applied by
  :func:`apply_reground_plan` only after adds commit and are read back
  from ``links`` (paired-add requirement, strand guard, partial-failure
  counts). A contradictor is re-attached as a non-blocking ``disputes``
  edge (docs/backlog/disputes-edge-nonblocking-disagreement.md D3), never
  plain-dropped.
* **External last resort + retire flagging** (stages 5-6) — each behind
  its own gate; retire additionally needs a per-hub opt-in tag, and its
  draft-prose edit is stubbed to a worklist flag (:func:`_flag_retire`).

**The unattended reground service loop is dark**:
:meth:`RegroundConfig.from_env` returns ``None`` unless
``PRECIS_TAPROOT_REGROUND`` — gating only ``hub_refine``'s own due-set
pickup, not the ``reground_claim`` job (``workers/job_types/
reground_claim.py``), an independent explicit-opt-in entry point that
dispatches regardless (a submitted job is already a human decision). The
prune sub-stage additionally needs ``PRECIS_TAPROOT_REGROUND_PRUNE`` set
to the literal :data:`PRUNE_INTERLOCK_TOKEN`, not a boolean — it must not
enable in prod until ``taproot.slice_refine_eval`` passes the deployed
strict rubric (over-prune is the dangerous direction; a plain ``=1`` is
what muscle memory flips). The interlock gates both paths
(:func:`prune_interlock_open`). Retire/regenerate has no env flag at all
— reachable only through the ``reground_claim`` job's explicit param plus
the hub tag.

Once claiming work, the pass always verifies with the LLM — no separate
``with_llm`` toggle (unlike ``chase``): a run that can't verify can't do
anything, so reaching this pass already implies paying for it. The one
hard dependency is the embedder (discovery needs a query vector); absent
one, the pass logs a warning and no-ops the whole cycle (mirrors the
forward bridge's own embedder-unavailable degrade).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.handlers._link_tag_ops import validate_relation
from precis.nanopub.demote import DemotionRequest, run_demotions
from precis.store.types import Tag
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    _parse_json_object,
    claim_hub_predicate_sql,
    claim_sha,
    not_hypothesis_predicate_sql,
)
from precis.taproot.hub import (
    HUB_ROLES,
    EvidenceHandle,
    WouldStrandHub,
    append_reground_log,
    attach_evidence,
    live_evidence_handles,
    reattach_as_disputes,
    reground_log_entry,
    remove_evidence,
    run_retraction_checks,
)
from precis.taproot.resolve import resolve_citation

# Acyclic on purpose: taproot.verify_edges imports only workers._chase_llm
# (a leaf this module already depends on) and taproot.canon — never this
# module or the workers package surface (workers/__init__ exports only the
# chunk-pass core), so reusing its cohort selectors here cannot cycle.
from precis.taproot.verify_edges import (
    select_unverified_stamped_edges,
    select_withheld_edges,
)
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.llm.router import LlmRequest, Tier, route
from precis.workers._chase_llm import _verify_support_with_caveats, is_corroborating

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The claim-hub tag predicate, pre-rendered for embedding into this
#: module's own ``WHERE`` clauses (:func:`~precis.taproot.canon.claim_hub_predicate_sql`
#: — the single source of truth for "is this ref a claim hub", not a
#: fourth reinvention of the pair of ``EXISTS`` clauses).
_CLAIM_HUB_SQL = claim_hub_predicate_sql()
#: The "not a conjecture" clause, AND-ed onto the claim-hub predicate below.
#: A hypothesis hub carries the same TAPROOT:claim + STATUS:canonical tags,
#: so without this the widening pass would go hunting for evidence that
#: supports a guess — a confirmation engine aimed at exactly the thing
#: nothing supports yet (docs/backlog/claim-review-mechanism.md).
_NOT_HYPOTHESIS_SQL = not_hypothesis_predicate_sql()

#: The evidence-edge role hub-refine always attaches with — never
#: ``establishes`` (originator promotion is derived elsewhere, see the
#: module docstring's "Originators are still derived" note in the build
#: ticket).
_ROLE = "corroborates"

#: The ``meta.verified_by`` fingerprint on every support verdict this pass
#: writes — distinct from ``taproot.verify_edges``'s ``'verify-edges'`` (the
#: standalone sweep) and from the mint paths' own fingerprints, so a verdict
#: is always traceable to the judge that issued it.
_VERIFIED_BY = "hub-refine"

#: Publish-gate re-verify budget: verifier calls per hub per pass over the
#: hub's own attached-but-unverified edges (:func:`_reverify_pinned_edges`).
#: Small on purpose, same shape as :data:`_PER_PAPER_K`: a hub carries a
#: handful of edges, so a typical hub certifies in one or two passes while
#: the per-hub LLM bound grows by a constant, not by the edge count.
_REVERIFY_PER_PASS = 4


def _verified_stamp(sha: str) -> dict[str, Any]:
    """The three-key verification fingerprint every support verdict this
    pass writes rides with — the same stamp shape as
    ``taproot.verify_edges._stamp_edge`` (modulo :data:`_VERIFIED_BY`), so
    the publish preflight and the verify-edges cohorts read both passes'
    verdicts identically. ``sha`` is the hub sentence's ``claim_sha`` at
    verify time: a later claim edit invalidates the verdict."""
    return {
        "verified_by": _VERIFIED_BY,
        "verified_at": datetime.now(UTC).isoformat(),
        "verified_claim_sha": sha,
    }


def _attach_disputes(
    store: Store,
    conn: Connection,
    *,
    hub_ref_id: int,
    source_ref_id: int,
    handle: str | None,
    reason: str | None,
    caveats: list[str],
    sha: str,
    via: str,
    pending_demotions: list[DemotionRequest] | None,
) -> None:
    """File one plain source ``--disputes-->`` hub open-question link from
    the enrichment arm, and queue the hub's demotion.

    Renamed from ``_attach_contradicts`` (docs/backlog/disputes-edge-
    nonblocking-disagreement.md D3): a contradicting judge verdict on a
    source never previously attached to this hub is no longer written as
    an adjudicated ``contradicts`` evidence edge — it's the free,
    non-blocking ``disputes`` open question, the same ``store.add_link`` +
    ``validate_relation('disputes')`` shape
    :func:`~precis.taproot.hub.reattach_as_disputes` uses for its own add
    half (idempotent on the DB's endpoint+relation unique index). ``widen``
    is the provenance marker, mirroring the reground path's
    ``meta.reground``, so every edge this arm writes stays queryable as a
    set. The LLM verdict vocabulary (``"CONTRADICTS"`` in ``meta.widen``)
    is unchanged — only the stored *link relation* moved.

    The demotion is *queued*, never applied here: this runs inside the
    caller's open transaction and ``nanopub_reopen`` opens its own pool
    connection (see :mod:`precis.nanopub.demote`). Demotion still fires on
    a disputes filing — the same precedent :func:`apply_reground_plan` set
    for its own contradicts->disputes conversion: a hub's publish posture
    no longer reflects its evidence the moment a primary-against passage
    surfaces, independent of whether the new edge itself blocks.
    """
    validated_relation = validate_relation("disputes", store=store)
    store.add_link(
        src_ref_id=source_ref_id,
        dst_ref_id=hub_ref_id,
        relation=validated_relation,
        meta={
            "support": "no",
            "support_reason": reason,
            "caveats": caveats,
            "source_handle": handle,
            "widen": {"verdict": "CONTRADICTS", "sha": sha, "via": via},
        },
        set_by="system",
        conn=conn,
    )
    if pending_demotions is not None:
        pending_demotions.append(
            DemotionRequest(
                hub_ref_id=hub_ref_id,
                reason=(
                    f"widening pass attached a disputes edge from source "
                    f"#{source_ref_id}"
                ),
            )
        )


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
    blake2b), so this scans the whole (~1.2k) canonical claim-hub set,
    computes due-ness in Python, then re-locks just the winning ``limit``
    ids with a second ``FOR UPDATE SKIP LOCKED`` — a concurrent pass
    already holding one of those rows drops it from the returned set.

    Pops each claimed hub's ``TAPROOT_DUE`` tag in this same call (a
    re-mark mid-processing simply re-triggers next pass), and writes each
    locked hub's :data:`_ATTEMPT_NS` claim-time lease in the same commit
    (see that constant's docstring).

    Excludes **compound** claim hubs (module docstring step 1) via a
    ``NOT EXISTS`` over an inbound live ``conjunct-of`` edge — the same
    predicate :func:`_is_compound_hub` checks per-hub, deliberately
    re-derived rather than shared (module docstring's "cross-task seam"
    note).
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
           AND r.retired_at IS NULL
           AND {_CLAIM_HUB_SQL}
           AND {_NOT_HYPOTHESIS_SQL}
           AND NOT EXISTS (
                 SELECT 1 FROM links l
                  JOIN refs a ON a.ref_id = l.src_ref_id
                 WHERE l.dst_ref_id = r.ref_id
                   AND l.relation = 'conjunct-of'
                   AND a.kind = 'finding'
                   AND a.retired_at IS NULL
               )
        """,
        {
            "due_ns": _DUE_NS,
            "due_value": _DUE_VALUE,
            "attempt_ns": _ATTEMPT_NS,
            **CLAIM_HUB_PREDICATE_PARAMS,
            **NOT_HYPOTHESIS_PREDICATE_PARAMS,
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
           AND a.retired_at IS NULL
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return row is not None


def _fetch_hub_info(conn: Connection, ref_id: int) -> tuple[str, dict[str, Any]] | None:
    """``(title, meta)`` for a live hub finding — ``None`` if it's gone."""
    row = conn.execute(
        "SELECT title, meta FROM refs WHERE ref_id = %s AND retired_at IS NULL",
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


def _rejected_source_ids(rejected: dict[str, Any]) -> set[int]:
    """Numeric source ref_ids from the rejection memo.

    The memo is keyed by ``str(source_ref_id)`` (JSON object keys are
    strings); a non-numeric key is stale hand-edited meta and is skipped
    rather than crashing the pass.
    """
    out: set[int] = set()
    for key in rejected:
        try:
            out.add(int(key))
        except (TypeError, ValueError):
            continue
    return out


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
            hits = store.chunks.search_chunks(
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
    """Filter a ``store.search_chunks`` hit list, dropping patent
    legal-claim-section blocks so they never become grounding candidates."""
    return [
        (block, ref, score)
        for block, ref, score in hits
        if block.meta.get("patent_block") != _PATENT_CLAIM_BLOCK
    ]


# ══ reground — the audit/prune/re-discover/escalate extension ═══════
#
# docs/backlog/taproot-reground.md. Everything below extends the SAME
# claim→discover→verify→write→stamp spine above; there is deliberately no
# parallel producer. All of it is DARK: :meth:`RegroundConfig.from_env`
# returns ``None`` unless an operator sets the flags, and the prune stage
# needs a *second* flag that must not be set in prod until
# ``taproot.slice_refine_eval`` passes on the deployed strict rubric (the
# spec's live blocker: hub 176363 must drop its contradicting partials,
# 176272/176360 must keep theirs — over-prune is the dangerous direction).


#: ``finding.meta`` keys the reground stages read/write. ``reground_seen``
#: is the **convergence guard**: ``{"<src_ref_id>:<chunk_id>": {"sha",
#: "verdict", "at"}}``, so any one edge (or re-offered passage of an
#: already-attached source) is judged at most once per ``claim_sha``.
#: Cleared by the same sha-reopen that clears ``taproot_rejected`` — an
#: edited claim genuinely reopens every verdict. Without this memo the
#: audit stage would re-judge every edge every pass, which is exactly the
#: unbounded re-scan the additive invariant existed to prevent.
_META_REGROUND_SEEN = "reground_seen"
#: The last external-escalation report (stage 5): mined reference DOIs we
#: do NOT hold, plus whatever the S2/Perplexity probe returned. Display +
#: worklist material, never itself evidence.
_META_REGROUND_EXTERNAL = "reground_external"
#: The hub's last reground verdict (``supportable`` / ``needs_external`` /
#: ``retire``) and, for a retire, its proposed one-sentence groundable
#: reword. A *record*, not a state machine — "questionable" stays emergent
#: (a hub at zero print-visible supporters reads ``unverified`` via
#: ``.trust``); nothing here is a new trust flag.
_META_REGROUND_VERDICT = "reground_verdict"
#: The intent-vs-committed repair anchor (spec §"The technique that found
#: the original damage"): the end state the last applied plan *intended*,
#: as ``{"sha", "at", "handles": [[src_ref_id, src_chunk_id, relation],
#: ...]}``. :func:`verify_hub_intent` diffs it against ``links`` with no
#: LLM spend at all, and :func:`repair_hub_intent` applies the delta
#: adds-first.
_META_REGROUND_INTENT = "reground_intent"

#: Per-hub opt-in for the **destructive** retire/regenerate sub-stage.
#: Two accepted forms, both checked (docs/backlog/taproot-reground.md's
#: "distinct flag for the sweep + a ``TAPROOT:reground-ok`` opt-in tag"):
#: the spec's literal ``TAPROOT:reground-ok``, for a human who hand-tags a
#: hub, and the side-channel ``TAPROOT_REGROUND_OK:1`` namespace that is
#: safe to write. The literal form is NOT the one to write from code: the
#: ``taproot`` axis (``data/axes/taproot.yaml``) is ``select: one`` over
#: ``[claim, review]``, so a re-classification pass would evict any third
#: ``TAPROOT:`` value. The side-channel namespace mirrors the
#: :data:`_DUE_NS` / :data:`_ATTEMPT_NS` precedent in this same module.
_REGROUND_OK_NS = "TAPROOT_REGROUND_OK"
_REGROUND_OK_VALUE = "1"
_REGROUND_OK_SPEC_NS = "TAPROOT"
_REGROUND_OK_SPEC_VALUE = "reground-ok"

#: Written by the retire stage when it fires: the hub is *flagged* for the
#: opus draft-prose pass, never auto-rewritten here (see
#: :func:`_flag_retire`'s docstring for exactly what is stubbed).
_REGROUND_RETIRE_NS = "TAPROOT_REGROUND_RETIRE"
_REGROUND_RETIRE_VALUE = "1"


def _env_flag(name: str) -> bool:
    """A ``PRECIS_*`` boolean knob, **default off**. Anything but an
    explicit ``1``/``true``/``yes``/``on`` (case-insensitive) is off — a
    typo'd flag must fail closed, because every knob in this section
    gates either LLM spend or a destructive write."""
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


#: The **exact** value ``PRECIS_TAPROOT_REGROUND_PRUNE`` must carry before
#: the unattended service will remove anything. Not a boolean, on purpose:
#: the prune stage is blocked on the ``taproot.slice_refine_eval`` rubric
#: gate (hub 176363 must drop its contradicting partials, 176272/176360
#: must keep theirs — over-prune is the dangerous direction, mirroring
#: canon's zero-false-``same``), and a plain ``=1`` is exactly the kind of
#: flag somebody flips from muscle memory while enabling the enrichment
#: half. Setting a token that names the precondition makes the operator
#: assert the eval passed, and makes the enablement greppable in a deploy
#: template. ``prune`` on the ``reground_claim`` job param remains the
#: normal, per-run, attended way to prune.
PRUNE_INTERLOCK_TOKEN = "eval-passed"


def prune_interlock_open() -> bool:
    """True iff ``PRECIS_TAPROOT_REGROUND_PRUNE`` carries
    :data:`PRUNE_INTERLOCK_TOKEN` **exactly** (no case folding — the point
    is a literal, greppable string in a deploy template, not a boolean).

    The single code-level answer to "has the rubric eval gate been
    signed off on this host?", checked by BOTH prune paths: the unattended
    service (:meth:`RegroundConfig.from_env`) and the attended
    ``reground_claim`` job param. Without this, ``params={'prune': true}``
    would be a way for any job submitter to run the prune stage in prod
    with the eval blocker unresolved — policy, not code. It is code now.
    """
    return (os.environ.get("PRECIS_TAPROOT_REGROUND_PRUNE") or "") == (
        PRUNE_INTERLOCK_TOKEN
    )


def _reground_deeper_topk() -> int:
    """``PRECIS_TAPROOT_REGROUND_TOPK`` — default **24**, deliberately
    deeper than enrichment's ``PRECIS_TAPROOT_REFINE_TOPK`` (8).

    Stage 3's whole premise: real primaries rank *low* while the
    ``references`` categorization is still converging (un-retagged
    bibliography paragraphs occupy the top slots), so the recall floor has
    to go deeper than enrichment used. Still a hard bound — reground stays
    converging-by-construction, it just converges from further down."""
    try:
        return int(os.environ.get("PRECIS_TAPROOT_REGROUND_TOPK", "24"))
    except ValueError:
        return 24


# ── the strict judge ────────────────────────────────────────────────

#: The three verdicts :func:`judge_edge_strict` may return.
STRICT_VERDICTS = ("KEEP", "PRUNE", "CONTRADICTS")


@dataclass(frozen=True)
class StrictVerdict:
    """One strict-judge answer about one passage."""

    verdict: str
    reason: str


#: Grounding-depth policy (fi189527, folded into this spec): what counts
#: as an acceptable *depth* of grounding depends on the claim's own shape.
DEPTH_ABSTRACT_OK = "abstract-ok"
DEPTH_BODY_REQUIRED = "body-required"

#: Any digit at all, or a mechanism/causation verb. Deliberately crude and
#: deliberately biased toward :data:`DEPTH_BODY_REQUIRED`: the cost of
#: mis-classifying a definition claim as measurement is that the judge is
#: asked for a body passage it may not need (and still defaults to KEEP on
#: uncertainty), whereas the cost of the opposite is accepting an
#: abstract's quoted number as primary support for a measurement — the
#: exact over-grounding the pilot found the minter doing.
_QUANTITY_RE = re.compile(r"\d")
_MECHANISM_RE = re.compile(
    r"(?i)\b(mechanis|cataly|induc|caus|because|via\b|due to|driv|"
    r"lead(?:s|ing)? to|result(?:s|ed|ing)? (?:in|from)|enabl|increas|"
    r"decreas|enhanc|suppress|improv|reduc|shift|convert|activat|inhibit)"
)


def claim_depth_policy(sentence: str) -> str:
    """:data:`DEPTH_BODY_REQUIRED` for a measurement/mechanism claim,
    :data:`DEPTH_ABSTRACT_OK` for a definition/existence one.

    Pure, no LLM — the policy is a *prompt input* and an attach-side
    filter, not itself a judgment. See :data:`_QUANTITY_RE` for why the
    heuristic leans strict.
    """
    s = sentence or ""
    if _QUANTITY_RE.search(s) or _MECHANISM_RE.search(s):
        return DEPTH_BODY_REQUIRED
    return DEPTH_ABSTRACT_OK


#: Section names that mark a chunk as front matter rather than a body
#: passage. Used only to keep a *new* attach from re-grounding a
#: measurement claim on an abstract/title/cover chunk — the pilot's
#: dominant failure ("right paper, wrong chunk"). This is NOT a
#: bibliography filter: reference chunks are excluded upstream by the
#: converging ``references`` categorizer (``bib_retag`` + the classifier),
#: which this pass deliberately does not duplicate.
_FRONT_MATTER_RE = re.compile(
    r"(?i)abstract|title|author|affiliation|cover|front ?matter|"
    r"acknowledg|keywords?|running head"
)


def is_front_matter(*, chunk_ord: int | None, section_path: str | None) -> bool:
    """True when a chunk reads as front matter: a front-matter section
    name, or ``ord == 0`` with no section at all (an untagged title/cover
    page — the shape the pilot kept finding under a measurement claim)."""
    if section_path and _FRONT_MATTER_RE.search(section_path):
        return True
    return chunk_ord == 0 and not section_path


_JUDGE_SYS = (
    "You are a skeptical scientific evidence auditor deciding whether a "
    "passage substantiates a claim with PRIMARY content or merely asserts "
    "it. Reply with ONLY the requested JSON object, no prose."
)

_DEPTH_NOTE = {
    DEPTH_BODY_REQUIRED: (
        "This is a MEASUREMENT / MECHANISM claim. An abstract-level or "
        "front-matter mention that merely quotes the result is NOT "
        "sufficient grounding — the primary is the body passage that "
        "describes the measurement, the computation, or the mechanism."
    ),
    DEPTH_ABSTRACT_OK: (
        "This is a DEFINITION / EXISTENCE claim. An abstract-level "
        "statement is acceptable primary grounding; do not demand a body "
        "passage for it."
    ),
}

_JUDGE_PROMPT = """\
You are auditing ONE existing evidence edge on a scientific claim hub.
Decide whether the PASSAGE substantiates the CLAIM with PRIMARY CONTENT,
or merely asserts it / defers to other people's work (a PROXY).

CLAIM:
{claim}

SETUP (structured):
{scope_json}

GROUNDING-DEPTH POLICY for this claim:
{depth_note}

SOURCE: {source_kind} {cite_key}, chunk ord {chunk_ord}{section_note}

PASSAGE (the edge's grounding chunk):
{chunk_text}

NEIGHBOURING PASSAGES in the same source (context only -- so you can tell
front matter from body. NEVER judge support off a neighbour):
{neighbours}

Answer with exactly one verdict:

  KEEP        : the passage carries PRIMARY content for the claim -- it
                reports the measurement, the computation, the mechanism,
                the observation, or the definition ITSELF, as the
                source's own work.
  PRUNE       : the passage does NOT substantiate the claim, because it is
                one of:
                  - an ASSERTION THAT DEFERS to other work (a review
                    sentence that states the claim while pointing at
                    uncited references, e.g. "[5-24]", with no data of
                    its own);
                  - a REVIEW / related-work / background recitation;
                  - a REVIEW-DEFERRAL of any other shape ("as has been
                    shown", "it is well known that") with no result here;
                  - an ABSTRACT-ONLY statement standing in for a
                    MEASUREMENT or MECHANISM claim (the number is quoted,
                    the measurement is not described);
                  - TITLE, BYLINE, author list, affiliation, cover page,
                    running header, or other FRONT MATTER;
                  - a BIBLIOGRAPHY / reference-list entry;
                  - text merely on-topic that never states the claim.
  CONTRADICTS : the passage carries PRIMARY content running COUNTER to
                the claim -- an opposite result, value, or tendency. Not
                "silent on part of it": actually against it.

RULES:
  - Judge the claim EXACTLY AS STATED, never a looser or more general
    version of it.
  - Be STRICTER than "does this sentence utter the claim?". A passage
    that utters the claim while attributing it to somebody else's
    references is a PROXY -> PRUNE.
  - DEFAULT TO KEEP WHEN UNCERTAIN. Over-pruning is the dangerous
    direction; a doubtful edge stays.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdict": "KEEP" | "PRUNE" | "CONTRADICTS",
  "reason": "<one sentence>"
}}
"""

#: Per-passage excerpt caps in the judge prompt — the passage itself gets
#: the same 4000-char budget ``_chase_llm._verify_support_with_caveats``
#: uses; a neighbour is context only and gets far less.
_JUDGE_PASSAGE_CHARS = 4000
_JUDGE_NEIGHBOUR_CHARS = 600


def judge_edge_strict(
    *,
    claim: str,
    scope: dict[str, Any],
    cite_key: str,
    chunk_ord: int | None,
    chunk_text: str,
    source_kind: str = "paper",
    depth_policy: str = DEPTH_BODY_REQUIRED,
    section_path: str | None = None,
    neighbours: list[str] | None = None,
) -> StrictVerdict | None:
    """The strict judge — stage 2's verdict on ONE passage.

    **Strictly stricter than** ``_chase_llm._verify_support_with_caveats``,
    which is the minter's verifier and returns *yes* on a proxy because
    the sentence states the claim. That looseness is correct for
    *attaching* (a scoped, hedged supporter is still a supporter); it is
    wrong for *auditing*, where the question is whether the passage is the
    primary at all. The two prompts are deliberately separate rather than
    one parameterized prompt: enrichment's calibration is load-bearing for
    every existing edge, and re-tuning it to serve the audit would move
    the attach threshold for the whole corpus.

    Returns ``None`` on a dispatch failure or an unparseable reply —
    treated by every caller as "no verdict", i.e. no memo written and no
    action taken, so the edge is simply re-judged next pass. Conflating a
    dead dispatch with a PRUNE would delete evidence on an infra blip.
    """
    section_note = f", section {section_path}" if section_path else ""
    neighbour_text = (
        "\n\n".join(n[:_JUDGE_NEIGHBOUR_CHARS] for n in neighbours if n)
        if neighbours
        else "(none — this passage has no live neighbours in the source)"
    )
    prompt = _JUDGE_PROMPT.format(
        claim=claim,
        scope_json=json.dumps(scope, sort_keys=True),
        depth_note=_DEPTH_NOTE.get(depth_policy, _DEPTH_NOTE[DEPTH_BODY_REQUIRED]),
        source_kind=source_kind,
        cite_key=cite_key,
        chunk_ord=chunk_ord if chunk_ord is not None else "(ref-level)",
        section_note=section_note,
        chunk_text=chunk_text[:_JUDGE_PASSAGE_CHARS],
        neighbours=neighbour_text,
    )
    res = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            messages=[
                {"role": "system", "content": _JUDGE_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="taproot:reground-judge",
        )
    )
    if res.error:
        log.warning("hub_refine: strict judge dispatch failed: %s", res.error)
        return None
    data = res.data if isinstance(res.data, dict) else _parse_json_object(res.text)
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in STRICT_VERDICTS:
        log.warning("hub_refine: strict judge returned verdict %r — ignored", verdict)
        return None
    return StrictVerdict(
        verdict=str(verdict), reason=str(data.get("reason") or "").strip()
    )


JudgeFn = Callable[..., "StrictVerdict | None"]
ExternalProbeFn = Callable[[str], list[dict[str, Any]]]


# ── stage 5: external last resort ───────────────────────────────────


def _probe_s2(claim_sentence: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Semantic Scholar free-text search for primary support.

    Goes through :func:`precis.ingest.semantic_scholar.search_s2_papers`
    (the existing corpus-side door, which owns its own HTTP client and
    degrades to ``[]`` on any error) rather than building a URL here — so
    this pass performs **no agent-supplied-URL fetch at all** and the
    ``safe_get``/``safe_stream`` requirement is met by construction, not
    by a wrapper that could be forgotten. Same reasoning for the DOI
    mining leg, which is pure SQL.
    """
    from precis.ingest.semantic_scholar import search_s2_papers

    out: list[dict[str, Any]] = []
    for paper in search_s2_papers(claim_sentence, limit=limit):
        doi = paper.get("doi")
        if not doi:
            continue
        out.append(
            {"doi": str(doi), "title": str(paper.get("title") or ""), "source": "s2"}
        )
    return out


def _probe_perplexity(claim_sentence: str) -> list[dict[str, Any]]:
    """STUB — the Perplexity leg of stage 5's external escalation.

    Intended shape: run the claim as a ``perplexity-research`` query
    through a booted :class:`precis.dispatch.Hub`
    (``ResearchHandler.get(q=…)``), regex DOIs out of the returned report,
    and hand them back in the same ``{doi, title, source}`` shape
    :func:`_probe_s2` uses so :func:`_external_candidates` can treat both
    legs identically.

    Deliberately not built in this pass: the handler is a paid,
    API-key-gated cache-backed kind whose ``get`` door would need booting
    (and billing) from inside a worker for a stage that is dark and has
    fired on **zero** of the pilot's ten hubs. The seam is the thing that
    matters — swap this in via ``RegroundConfig.external_probe_fn`` (or
    replace this body) and nothing else changes. Returning ``[]`` degrades
    stage 5 to its S2 + DOI-mining legs, which is exactly what a dead
    probe should do.
    """
    return []


def _probe_external(claim_sentence: str) -> list[dict[str, Any]]:
    """The default external probe: both legs, deduped by DOI. Each leg is
    independently best-effort — a dead probe degrades the stage, never
    fails the hub."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for leg in (_probe_s2, _probe_perplexity):
        try:
            hits = leg(claim_sentence)
        except Exception:
            log.warning(
                "hub_refine: external probe %s failed", leg.__name__, exc_info=True
            )
            continue
        for hit in hits:
            doi = str(hit.get("doi") or "").strip().lower()
            if not doi or doi in seen:
                continue
            seen.add(doi)
            out.append(hit)
    return out


def _external_candidates(
    conn: Connection,
    store: Store,
    hub_ref_id: int,
    *,
    claim_sentence: str,
    query_vec: list[float],
    grounding_ref_ids: list[int],
    probe_fn: ExternalProbeFn,
) -> tuple[list[_Candidate], dict[str, Any]]:
    """Stage 5. Returns ``(candidates, report)``.

    Two sources, both reduced to the same question — *is the primary
    already in the corpus under a DOI nobody linked?*

    1. **Reference-list DOI mining**: the hub's own grounding papers'
       parsed bibliographies (``paper_bib_entries``, migration 0108). An
       entry already matched to a held ref (``held_ref_id``) becomes an
       ordinary candidate via a paper-scoped semantic search — the claim's
       primary is often one hop down its own proxy's reference list.
    2. **Probe** (``probe_fn``, S2 by default): free-text search for
       papers whose DOI we may already hold.

    A DOI we do **not** hold is recorded in the report, never acquired
    here. Auto-acquisition (``put(kind='paper', doi=…)``) is a separate,
    unbounded, network+cost-bearing decision, and the spec's own residual
    (fi189542's paywalled Krishnan 1997) is precisely a human call. This
    is the one place stage 5 is deliberately short of "resolve → hold".
    """
    report: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "unheld_dois": [],
        "probed": [],
    }
    candidates: list[_Candidate] = []
    if not grounding_ref_ids:
        return candidates, report

    rows = conn.execute(
        "SELECT DISTINCT doi, held_ref_id FROM paper_bib_entries "
        "WHERE ref_id = ANY(%s) AND doi IS NOT NULL",
        (grounding_ref_ids,),
    ).fetchall()
    held_by_doi: dict[str, int] = {}
    for doi, held_ref_id in rows:
        doi_s = str(doi).strip().lower()
        if not doi_s:
            continue
        if held_ref_id is None:
            report["unheld_dois"].append({"doi": doi_s, "via": "reference-list"})
        else:
            held_by_doi[doi_s] = int(held_ref_id)

    probed = probe_fn(claim_sentence)
    report["probed"] = probed
    for hit in probed:
        doi_s = str(hit.get("doi") or "").strip().lower()
        if not doi_s or doi_s in held_by_doi:
            continue
        # A ref's DOI lives in ``ref_identifiers`` (id_kind='doi',
        # lower-cased by a CHECK), not on ``refs`` — same read shape
        # ``store._refs_ops`` uses.
        row = conn.execute(
            "SELECT r.ref_id FROM refs r JOIN ref_identifiers ri "
            "ON ri.ref_id = r.ref_id AND ri.id_kind = 'doi' "
            "WHERE r.kind = 'paper' AND r.retired_at IS NULL "
            "AND ri.id_value = %s LIMIT 1",
            (doi_s,),
        ).fetchone()
        if row is None:
            report["unheld_dois"].append({"doi": doi_s, "via": hit.get("source")})
        else:
            held_by_doi[doi_s] = int(row[0])

    for ref_id in sorted(set(held_by_doi.values())):
        if ref_id == hub_ref_id:
            continue
        hits = store.chunks.search_chunks(
            q=claim_sentence,
            query_vec=query_vec,
            mode="semantic",
            kind="paper",
            scope_ref_id=ref_id,
            limit=1,
            max_distance=None,
        )
        if hits:
            block, ref, _score = hits[0]
            candidates.append(_Candidate(block=block, ref=ref, via="external"))
    return candidates, report


# ── plan / apply shapes ─────────────────────────────────────────────


@dataclass(frozen=True)
class RegroundAdd:
    """One evidence edge this pass attached (already committed with the
    hub's enrichment transaction) — the applier's read-back target."""

    src_ref_id: int
    src_chunk_id: int | None
    handle: str | None = None
    via: str = "semantic"


@dataclass(frozen=True)
class RegroundPrune:
    """One edge the strict judge rejected, held for the post-commit
    applier. ``requires_replacement`` is the add-first contract: a
    depth-correction prune (the pilot's dominant, low-risk case) is only
    released once its replacement add is confirmed committed."""

    src_ref_id: int
    src_chunk_id: int | None
    relation: str
    reason: str
    verdict: str = "PRUNE"
    handle: str | None = None
    requires_replacement: bool = True


@dataclass(frozen=True)
class RegroundContradict:
    """One edge whose passage carries primary content *against* the claim
    — converted to a non-blocking ``disputes`` edge (ADR 0073, repointed by
    docs/backlog/disputes-edge-nonblocking-disagreement.md D3), never
    plain-dropped."""

    src_ref_id: int
    src_chunk_id: int | None
    relation: str
    reason: str
    handle: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


#: :attr:`RegroundPlan.verdict` values. ``supportable`` — the hub holds
#: (or now holds) primary grounding; ``needs_external`` — the corpus
#: could not substantiate it and stage 5 either did not run or found
#: nothing; ``retire`` — nothing anywhere substantiates the claim as
#: worded. None of these is a stored trust state: "questionable" stays
#: emergent (a hub at zero print-visible supporters reads ``unverified``
#: via ``.trust``).
VERDICT_SUPPORTABLE = "supportable"
VERDICT_NEEDS_EXTERNAL = "needs_external"
VERDICT_RETIRE = "retire"


@dataclass
class RegroundPlan:
    """One hub's reground outcome: what was added (already committed) and
    what still has to be removed, plus the intended end state.

    Mutable on purpose — :func:`_refine_one_hub` fills it in as the
    stages run, then hands it to :func:`apply_reground_plan` *after* its
    own transaction commits. That split is the add-first contract in
    structural form: adds land with the enrichment transaction, prunes
    cannot even be attempted until that commit is visible on a fresh
    connection.
    """

    hub_ref_id: int
    claim_sha: str
    adds: list[RegroundAdd] = field(default_factory=list)
    prunes: list[RegroundPrune] = field(default_factory=list)
    contradicts: list[RegroundContradict] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    verdict: str = VERDICT_SUPPORTABLE
    reword: str | None = None
    #: Live edges at plan time, before anything in this plan was applied.
    live_before: set[EvidenceHandle] = field(default_factory=set)
    #: Whether stage 5 actually ran for this hub — the difference between
    #: "the external last resort found nothing" (``retire``) and "we never
    #: asked" (``needs_external``). A gate being *enabled* is not the same
    #: as the stage having *fired*.
    external_ran: bool = False

    def intended_end_state(self) -> set[EvidenceHandle]:
        """The edge set this plan means the hub to end up with — the left
        side of the intent-vs-committed diff. Rebuilt, never stored as a
        sort order or any other new state: current live edges, minus the
        planned prunes, minus the corroborates edges being converted, plus
        this plan's adds. A ``disputes`` conversion (:attr:`contradicts`)
        only discards the old edge — the replacement is a plain
        ``disputes`` link (docs/backlog/disputes-edge-nonblocking-
        disagreement.md D3), not a :data:`HUB_ROLES` evidence edge, so it
        never appears in :func:`~precis.taproot.hub.live_evidence_handles`
        and has nothing to add here."""
        end = set(self.live_before)
        for p in self.prunes:
            end.discard(EvidenceHandle(p.src_ref_id, p.src_chunk_id, p.relation))
        for c in self.contradicts:
            end.discard(EvidenceHandle(c.src_ref_id, c.src_chunk_id, c.relation))
        for a in self.adds:
            end.add(EvidenceHandle(a.src_ref_id, a.src_chunk_id, _ROLE))
        return end


@dataclass(frozen=True)
class RegroundApplyResult:
    """What :func:`apply_reground_plan` actually did — the **partial-
    failure surface** the spec requires: a caller reading only the summary
    must still see that something was withheld."""

    hub_ref_id: int
    confirmed_adds: int = 0
    missing_adds: int = 0
    pruned: int = 0
    withheld: int = 0
    contradicts_reattached: int = 0
    #: Publish-row demotions this applier landed. The prune stage's
    #: contradicts *conversions* run here, after the refine transaction
    #: has committed, so they demote inline rather than through the
    #: caller's ``pending_demotions`` queue — without this field they
    #: would be invisible to the pass summary's ``demoted`` counter.
    demoted: int = 0
    stranded_refused: bool = False
    flags: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.missing_adds or self.withheld or self.stranded_refused)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hub": self.hub_ref_id,
            "confirmed_adds": self.confirmed_adds,
            "missing_adds": self.missing_adds,
            "pruned": self.pruned,
            "withheld": self.withheld,
            "contradicts_reattached": self.contradicts_reattached,
            "demoted": self.demoted,
            "stranded_refused": self.stranded_refused,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class RegroundConfig:
    """Which reground stages are live for this run. Every field defaults
    to the SAFE value; :meth:`from_env` returns ``None`` (reground off
    entirely) unless an operator opts in.

    ``prune`` is the one that must not be flipped in prod until
    ``taproot.slice_refine_eval`` passes on the deployed strict rubric —
    that eval is the gate, this flag is only the switch behind it. Both
    routes to setting it (this class's :meth:`from_env` and the
    ``reground_claim`` job param) go through :func:`prune_interlock_open`,
    so the blocker is enforced in code on every path rather than by
    convention on one of them.
    ``authorize_retire`` additionally requires the per-hub opt-in tag
    (:func:`_retire_authorized`), so the destructive stage can never fire
    on a hub nobody vetted.
    """

    audit: bool = True
    prune: bool = False
    external: bool = False
    authorize_retire: bool = False
    deeper_topk: int = 24
    judge_fn: JudgeFn = judge_edge_strict
    external_probe_fn: ExternalProbeFn = _probe_external

    @classmethod
    def from_env(cls) -> RegroundConfig | None:
        """``None`` unless ``PRECIS_TAPROOT_REGROUND`` is set — the whole
        extension is dark by default. ``PRECIS_TAPROOT_REGROUND_EXTERNAL``
        is a separate, additional opt-in;
        ``PRECIS_TAPROOT_REGROUND_PRUNE`` is stricter still and must carry
        :data:`PRUNE_INTERLOCK_TOKEN` verbatim rather than a boolean (see
        that constant — the prune stage is blocked on the
        ``slice_refine_eval`` rubric gate). There is deliberately **no**
        env flag for retire/regenerate: that stage is reachable only
        through the ``reground_claim`` job's explicit ``authorize_retire``
        param plus the per-hub tag, so an env flip on a worker host can
        never turn it on."""
        if not _env_flag("PRECIS_TAPROOT_REGROUND"):
            return None
        return cls(
            prune=prune_interlock_open(),
            external=_env_flag("PRECIS_TAPROOT_REGROUND_EXTERNAL"),
            deeper_topk=_reground_deeper_topk(),
        )


# ── stage 1/2: fisheye assemble + audit ─────────────────────────────


def _seen_key(src_ref_id: int, src_chunk_id: int | None) -> str:
    """The convergence memo's key — source ref plus grounding chunk, so a
    *different* passage of an already-attached source is a distinct thing
    to judge (that is the whole depth-correction move) while the same
    passage is never re-judged at one ``claim_sha``."""
    return f"{src_ref_id}:{src_chunk_id if src_chunk_id is not None else '-'}"


@dataclass(frozen=True)
class _FisheyeEdge:
    """One current evidence edge with the context stage 1 assembles: its
    grounding chunk plus that chunk's prev/next ``ord`` neighbours in the
    same source — "the fisheye on all the pcs we point to"."""

    src_ref_id: int
    src_chunk_id: int | None
    relation: str
    chunk_ord: int | None
    chunk_text: str
    section_path: str | None
    source_kind: str
    cite_key: str
    neighbours: list[str]

    @property
    def handle(self) -> str | None:
        return handle_registry.try_format(
            self.source_kind, self.src_chunk_id, chunk=True
        )


def _fisheye_edges(conn: Connection, hub_ref_id: int) -> list[_FisheyeEdge]:
    """Stage 1 — every current grounding chunk of ``hub_ref_id`` with its
    neighbours. One query for the edges, one per edge for the
    neighbour pair; a hub carries a handful of edges, so this is bounded
    by the same small constant the rest of the pass is."""
    rows = conn.execute(
        """
        SELECT l.src_ref_id, l.src_chunk_id, l.relation,
               c.ord, c.text, array_to_string(c.section_path, ' > '),
               r.kind,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1) AS cite_key
          FROM links l
          JOIN refs r ON r.ref_id = l.src_ref_id
          LEFT JOIN chunks c ON c.chunk_id = l.src_chunk_id
         WHERE l.dst_ref_id = %s AND l.relation = ANY(%s)
         ORDER BY l.src_ref_id, l.src_chunk_id
        """,
        (hub_ref_id, sorted(HUB_ROLES)),
    ).fetchall()
    out: list[_FisheyeEdge] = []
    for src_ref_id, src_chunk_id, relation, ord_, text, section, kind, slug in rows:
        neighbours: list[str] = []
        if ord_ is not None:
            nb_rows = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = ANY(%s) "
                "AND retired_at IS NULL AND ord >= 0 ORDER BY ord",
                (int(src_ref_id), [int(ord_) - 1, int(ord_) + 1]),
            ).fetchall()
            neighbours = [str(r[0] or "") for r in nb_rows]
        out.append(
            _FisheyeEdge(
                src_ref_id=int(src_ref_id),
                src_chunk_id=int(src_chunk_id) if src_chunk_id is not None else None,
                relation=str(relation),
                chunk_ord=int(ord_) if ord_ is not None else None,
                chunk_text=str(text or ""),
                section_path=str(section) if section else None,
                source_kind=str(kind),
                cite_key=str(slug or f"ref:{src_ref_id}"),
                neighbours=neighbours,
            )
        )
    return out


def _audit_edges(
    conn: Connection,
    hub_ref_id: int,
    *,
    claim_sentence: str,
    scope: dict[str, Any],
    depth_policy: str,
    cfg: RegroundConfig,
    seen: dict[str, Any],
    plan: RegroundPlan,
) -> None:
    """Stage 2 — strict-judge every current supporter, memoing each
    verdict at the current ``claim_sha``.

    Mutates ``seen`` (the convergence memo) and ``plan``. A ``KEEP`` lands
    in the memo only: logging every kept edge every pass would turn the
    audit trail into a heartbeat. Only actions (prune / contradicts) reach
    ``meta.reground_log``.

    Note what does **not** happen here: nothing is removed. Prunes are
    *planned*, and applied only after the hub's transaction commits and
    the paired adds have been read back — see :func:`apply_reground_plan`.
    """
    if not cfg.audit:
        return
    for edge in _fisheye_edges(conn, hub_ref_id):
        if not edge.chunk_text.strip():
            # Ref-level edge (or a stale source_handle that resolves to no
            # live chunk — fi191322's shape). There is no passage to judge;
            # a removal here would be a guess, so it is left alone.
            continue
        key = _seen_key(edge.src_ref_id, edge.src_chunk_id)
        if seen.get(key, {}).get("sha") == plan.claim_sha:
            continue  # convergence guard: judged once per claim_sha
        verdict = cfg.judge_fn(
            claim=claim_sentence,
            scope=scope,
            cite_key=edge.cite_key,
            chunk_ord=edge.chunk_ord,
            chunk_text=edge.chunk_text,
            source_kind=edge.source_kind,
            depth_policy=depth_policy,
            section_path=edge.section_path,
            neighbours=edge.neighbours,
        )
        if verdict is None:
            # No verdict (dispatch failure / unparseable) — deliberately
            # NOT memoed, so the edge is re-judged next pass rather than
            # being silently frozen at an accident.
            continue
        seen[key] = {
            "sha": plan.claim_sha,
            "verdict": verdict.verdict,
            "at": datetime.now(UTC).isoformat(),
        }
        if verdict.verdict == "KEEP":
            continue
        if verdict.verdict == "CONTRADICTS":
            if edge.relation == "contradicts":
                continue  # already recorded as a contradictor
            plan.contradicts.append(
                RegroundContradict(
                    src_ref_id=edge.src_ref_id,
                    src_chunk_id=edge.src_chunk_id,
                    relation=edge.relation,
                    reason=verdict.reason,
                    handle=edge.handle,
                    meta={
                        "support": "no",
                        "caveats": [],
                        "source_handle": edge.handle,
                        "reground": {
                            "verdict": "CONTRADICTS",
                            "reason": verdict.reason,
                            "sha": plan.claim_sha,
                        },
                    },
                )
            )
            continue
        # PRUNE — planned, not applied. requires_replacement encodes the
        # pilot's finding that the safe, dominant action is a same-paper
        # depth correction: the prune rides on a confirmed replacement.
        if not cfg.prune:
            plan.flags.append(f"prune-proposed-but-gated:{edge.src_ref_id}")
            plan.log.append(
                reground_log_entry(
                    src_ref_id=edge.src_ref_id,
                    src_chunk_id=edge.src_chunk_id,
                    relation=edge.relation,
                    verdict="PRUNE",
                    reason=verdict.reason,
                    action="withheld (prune stage disabled)",
                    sha=plan.claim_sha,
                    handle=edge.handle,
                )
            )
            continue
        plan.prunes.append(
            RegroundPrune(
                src_ref_id=edge.src_ref_id,
                src_chunk_id=edge.src_chunk_id,
                relation=edge.relation,
                reason=verdict.reason,
                handle=edge.handle,
            )
        )


# ── stage 3: same-paper deeper-chunk re-discovery ───────────────────

#: The evidence-source kinds :func:`_same_paper_candidates` can run a
#: ``store.search_chunks`` leg against. A subset of
#: ``taproot.hub.EVIDENCE_SRC_KINDS`` on purpose — the same two kinds the
#: enrichment semantic source already searches.
_SEARCHABLE_SRC_KINDS = frozenset({"paper", "patent"})


def _same_paper_candidates(
    store: Store,
    *,
    hub_ref_id: int,
    source_refs: list[tuple[int, str]],
    claim_sentence: str,
    query_vec: list[float],
    per_paper_k: int,
) -> list[_Candidate]:
    """The **primary reground move**: deeper passages inside papers this
    hub is ALREADY grounded on.

    The pilot's dominant failure was "right paper, wrong chunk" — 5 of 6
    proxy prunes were same-paper depth corrections, where the real primary
    (measured values, a figure caption enumerating the result, the DFT
    body passage) sat deeper in an already-vetted paper. That makes this
    the lowest-risk and highest-yield source, so its candidates are
    offered *first*, ahead of citation-following and the corpus-wide ANN.

    A paper-scoped ``store.search_chunks`` per attached source, with no
    ``max_distance`` floor — the corpus-wide floor governs *discovery of
    new papers*; inside a paper we already accepted, the strict judge is
    the gate.
    """
    out: list[_Candidate] = []
    for ref_id, kind in source_refs:
        if ref_id == hub_ref_id or kind not in _SEARCHABLE_SRC_KINDS:
            # ``edgar``/``datasheet`` are evidence-source kinds
            # (``taproot.hub.EVIDENCE_SRC_KINDS``) but have no
            # ``search_chunks`` leg here; their edges are still audited,
            # they just get no deeper-passage re-discovery.
            continue
        hits = store.chunks.search_chunks(
            q=claim_sentence,
            query_vec=query_vec,
            mode="semantic",
            kind=kind,
            scope_ref_id=ref_id,
            limit=per_paper_k,
            max_distance=None,
        )
        if kind == "patent":
            hits = _drop_patent_claim_blocks(hits)
        for block, ref, _score in hits:
            out.append(_Candidate(block=block, ref=ref, via="same-paper"))
    return out


def _attached_source_refs(conn: Connection, hub_ref_id: int) -> list[tuple[int, str]]:
    """``(ref_id, kind)`` for every source already carrying an evidence
    edge on this hub — the input to :func:`_same_paper_candidates`, and
    the reason it needs the kind: ``store.search_chunks``' mode-dispatched
    wrapper takes one ``kind=`` string, and a patent must be searched as a
    patent (so :func:`_drop_patent_claim_blocks` still applies)."""
    rows = conn.execute(
        "SELECT DISTINCT l.src_ref_id, r.kind FROM links l "
        "JOIN refs r ON r.ref_id = l.src_ref_id "
        "WHERE l.dst_ref_id = %s AND l.relation = ANY(%s) "
        "AND r.retired_at IS NULL",
        (hub_ref_id, sorted(HUB_ROLES)),
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _attached_edge_keys(conn: Connection, hub_ref_id: int) -> set[str]:
    """:func:`_seen_key`\\ s of the passages this hub is already grounded
    at — a candidate landing on one of them is the *existing edge*, which
    the audit stage owns; re-offering it to discovery would double-judge
    it."""
    return {
        _seen_key(h.src_ref_id, h.src_chunk_id)
        for h in live_evidence_handles(conn, hub_ref_id)
    }


def _candidate_settled(
    *,
    source_ref_id: int,
    chunk_id: int | None,
    attached: set[int],
    attached_keys: set[str],
    rejected: dict[str, Any],
    reground: RegroundConfig | None,
    seen: dict[str, Any],
    sha: str,
) -> bool:
    """The Filter step's pre-check — **the memo-gated re-verify** the spec
    asks for at the old ``attached``-source pre-filter.

    Additive-only enrichment (``reground is None``) keeps its original
    behaviour byte-for-byte: a source ref already carrying *any* evidence
    edge is skipped outright, which is what made the pass converge. With
    reground on, that blanket skip is exactly what prevents the dominant
    fix (a deeper passage of an already-attached paper), so it narrows to:
    skip this source's *already-grounded passages*, and skip anything
    already judged at this ``claim_sha``. Convergence is preserved by the
    memo rather than by never looking — and a sha-reopen clears the memo,
    so an edited claim genuinely re-opens the question.
    """
    if str(source_ref_id) in rejected:
        return True
    if source_ref_id not in attached:
        return False
    if reground is None:
        return True
    key = _seen_key(source_ref_id, chunk_id)
    if key in attached_keys:
        return True
    return bool(seen.get(key, {}).get("sha") == sha)


# ── stage 6: retire / regenerate (gated, and stubbed at the prose edit)


def _retire_authorized(conn: Connection, hub_ref_id: int, cfg: RegroundConfig) -> bool:
    """Both gates, ANDed: the run-level ``authorize_retire`` (a
    ``reground_claim`` job param — deliberately **not** an env flag, so no
    worker-host env edit can turn this on) *and* the per-hub opt-in tag,
    so the destructive stage never fires on a hub nobody vetted."""
    if not cfg.authorize_retire:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND ((t.namespace = %s AND t.value = %s)
             OR (t.namespace = %s AND t.value = %s))
         LIMIT 1
        """,
        (
            hub_ref_id,
            _REGROUND_OK_NS,
            _REGROUND_OK_VALUE,
            _REGROUND_OK_SPEC_NS,
            _REGROUND_OK_SPEC_VALUE,
        ),
    ).fetchone()
    return row is not None


def _inbound_draft_citers(conn: Connection, hub_ref_id: int) -> list[int]:
    """Draft chunks whose prose carries this hub's ``[fi<id>]`` inline
    marker — the "retiring without editing the sentence leaves an
    unsupported assertion + a dangling cite" set. Returned so the prose
    pass (and a human) can see every artifact that would be left dangling,
    including the other-artifact citers the spec says are flagged, never
    silently rewritten."""
    rows = conn.execute(
        "SELECT c.chunk_id FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
        "WHERE r.kind = 'draft' AND r.retired_at IS NULL AND c.ord >= 0 "
        "AND c.retired_at IS NULL AND c.text LIKE %s ORDER BY c.chunk_id",
        (f"%[fi{hub_ref_id}]%",),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _flag_retire(
    conn: Connection, store: Store, hub_ref_id: int, plan: RegroundPlan
) -> None:
    """STUB (deliberate, flagged): flag the hub for the opus draft-prose
    pass instead of performing the prose edit.

    **Built here**: the gate (:func:`_retire_authorized`), the citer
    census (:func:`_inbound_draft_citers`), a durable
    ``meta.reground_verdict`` record carrying the proposed one-sentence
    groundable reword, the ``TAPROOT_REGROUND_RETIRE`` worklist tag, and a
    ``reground_log`` entry — everything a reviewer needs to act, and
    everything reversible.

    **Not built**: the three per-paragraph draft-edit modes themselves
    (reword-in-place / replace-with-a-supportable-fact / stitch-delete).
    Those require reading the whole surrounding paragraph, a frontier
    model, and a reversible draft-diff surface for end-review — a
    self-contained build of its own. The hub is left standing, its cites
    intact, on a worklist. Nothing is deleted, so the stub cannot leave a
    dangling cite; the failure mode of stopping here is a hub that stays
    ``unverified``, which is the correct resting state for an unsupported
    claim anyway.
    """
    citers = _inbound_draft_citers(conn, hub_ref_id)
    store.update_ref(
        hub_ref_id,
        meta_patch={
            _META_REGROUND_VERDICT: {
                "verdict": VERDICT_RETIRE,
                "sha": plan.claim_sha,
                "at": datetime.now(UTC).isoformat(),
                "reword": plan.reword,
                "draft_citers": citers,
                "prose_pass": "pending (draft-edit path not implemented)",
            }
        },
        conn=conn,
    )
    store.add_tag(
        hub_ref_id,
        Tag.closed(_REGROUND_RETIRE_NS, _REGROUND_RETIRE_VALUE),
        set_by="system",
        conn=conn,
    )
    plan.flags.append("retire-flagged")
    plan.log.append(
        reground_log_entry(
            src_ref_id=hub_ref_id,
            src_chunk_id=None,
            relation="(hub)",
            verdict=VERDICT_RETIRE,
            reason=(
                "no primary support in corpus or external sources; flagged "
                f"for the draft-prose pass ({len(citers)} draft citer(s))"
            ),
            action="retire-flagged",
            sha=plan.claim_sha,
        )
    )


# ── the applier: add-first, in code ─────────────────────────────────


def _pair_prunes_with_adds(
    prunes: list[RegroundPrune], confirmed: list[RegroundAdd]
) -> tuple[list[RegroundPrune], list[RegroundPrune]]:
    """Match each replacement-requiring prune to **one distinct** confirmed
    add. Returns ``(releasable, withheld)``.

    The spec is literal about this — *"issue a prune only for the subset
    whose replacement add is confirmed committed"* — so the pairing is
    **1:1**, not "any add releases every prune". Getting that wrong is the
    quiet version of the original 173020 damage: a hub with two proxy
    edges where only one gets a deeper replacement would have both pruned,
    losing the second paper's evidence with nothing put in its place. It
    would not strand the hub (the batch strand check still holds), so
    nothing would ever have complained.

    Two passes, because greedy first-fit would let a cross-paper prune
    steal an add that a later same-paper prune needs:

    1. **Same-source first** — a depth correction (the pilot's dominant,
       lowest-risk action: the replacement is another passage of the very
       paper being re-pointed) claims its own paper's add.
    2. **Leftovers** — a cross-paper swap (the rarer tail, e.g. fi191169)
       consumes one of the still-unclaimed adds.

    A prune with ``requires_replacement=False`` is released unconditionally
    — that is the *deliberate* unsupportable-claim prune, which the strand
    guard and the retire gate govern instead.
    """
    unclaimed = list(confirmed)
    releasable: list[RegroundPrune] = []
    pending: list[RegroundPrune] = []

    for p in prunes:
        if not p.requires_replacement:
            releasable.append(p)
            continue
        match = next((a for a in unclaimed if a.src_ref_id == p.src_ref_id), None)
        if match is None:
            pending.append(p)
            continue
        unclaimed.remove(match)
        releasable.append(p)

    withheld: list[RegroundPrune] = []
    for p in pending:
        if not unclaimed:
            withheld.append(p)
            continue
        unclaimed.pop(0)
        releasable.append(p)
    return releasable, withheld


def apply_reground_plan(
    store: Store, plan: RegroundPlan, *, set_by: str = "system"
) -> RegroundApplyResult:
    """Apply one hub's planned prunes — **after** its adds have committed.

    The four deterministic requirements from docs/backlog/taproot-reground.md
    §"Applier must enforce add-first in code, not in a prompt":

    1. **Read back** every add from ``links`` on a fresh connection — what
       counts is what's in the table, not what the plan's ``adds`` asked
       ``attach_evidence`` to write.
    2. **Prune only what has a confirmed replacement**, matched **1:1**
       (:func:`_pair_prunes_with_adds`): each replacement-requiring prune
       claims one distinct confirmed add — its own source's if there is
       one (depth correction), else a leftover (cross-paper swap). A
       prune with no add left to claim is **withheld and the hub
       flagged** — never silently skipped.
    3. **Re-check ``count(live edges) > 0``** — twice: the plan is
       simulated against read-back state before anything is deleted, and
       ``taproot.hub.remove_evidence`` independently refuses its own
       last-edge removal inside the transaction.
    4. **Surface partial-failure counts** — :class:`RegroundApplyResult`
       carries ``missing_adds``/``withheld``/``stranded_refused``/``flags``;
       the job glue puts them in the summary.

    Each prune runs in its own transaction so one bad handle degrades one
    edge, not the hub's whole plan. Disputes conversions run first
    (add-first *within* one transaction, ``taproot.hub.reattach_as_disputes``
    — writes a non-blocking ``disputes`` edge, not ``contradicts``, per
    docs/backlog/disputes-edge-nonblocking-disagreement.md D3) — the old
    edge's removal still changes the strand arithmetic below, even though
    the replacement no longer counts as a live :data:`HUB_ROLES` edge
    itself.
    """
    flags = list(plan.flags)
    log_entries = list(plan.log)
    reattached = 0
    withheld = 0

    for c in plan.contradicts:
        try:
            ok = reattach_as_disputes(
                store,
                hub_ref_id=plan.hub_ref_id,
                src_ref_id=c.src_ref_id,
                src_chunk_id=c.src_chunk_id,
                from_role=c.relation,
                reason=c.reason,
                claim_sha=plan.claim_sha,
                handle=c.handle,
                meta=c.meta,
                set_by=set_by,
            )
        except Exception:
            log.warning(
                "hub_refine: disputes re-attach failed for hub #%d src #%d",
                plan.hub_ref_id,
                c.src_ref_id,
                exc_info=True,
            )
            ok = False
        if ok:
            reattached += 1
        else:
            withheld += 1
            flags.append(f"disputes-reattach-failed:{c.src_ref_id}")

    demoted = 0
    if reattached:
        # The applier runs after the refine transaction has committed, so
        # the demotion can be applied inline rather than queued. Counted
        # onto the result so the pass summary sees a prune-stage demotion
        # the same way it sees a discover-stage one.
        demoted = sum(
            1
            for d in run_demotions(
                store,
                [
                    DemotionRequest(
                        hub_ref_id=plan.hub_ref_id,
                        reason=(
                            f"reground converted {reattached} evidence edge(s) to "
                            f"disputes"
                        ),
                    )
                ],
            )
            if d.applied
        )

    with store.pool.connection() as conn:
        committed = live_evidence_handles(conn, plan.hub_ref_id)

    confirmed = [
        a
        for a in plan.adds
        if EvidenceHandle(a.src_ref_id, a.src_chunk_id, _ROLE) in committed
    ]
    missing = [a for a in plan.adds if a not in confirmed]
    for a in missing:
        flags.append(f"add-not-committed:{a.src_ref_id}:{a.src_chunk_id}")

    live_prunes = [
        p
        for p in plan.prunes
        if EvidenceHandle(p.src_ref_id, p.src_chunk_id, p.relation) in committed
        # anything else is already gone (a prior partial run) — nothing to do
    ]
    eligible, unpaired = _pair_prunes_with_adds(live_prunes, confirmed)
    for p in unpaired:
        withheld += 1
        flags.append(f"prune-withheld-no-confirmed-add:{p.src_ref_id}")
        log_entries.append(
            reground_log_entry(
                src_ref_id=p.src_ref_id,
                src_chunk_id=p.src_chunk_id,
                relation=p.relation,
                verdict=p.verdict,
                reason=p.reason,
                action="withheld (no confirmed replacement add)",
                sha=plan.claim_sha,
                handle=p.handle,
            )
        )

    stranded_refused = False
    if eligible and len(committed) - len(eligible) <= 0:
        # Simulated end state is zero live edges. Refuse the whole batch:
        # a hub SHOULD be able to reach zero, but only via an authorized
        # retire, never as the residue of a half-applied plan.
        stranded_refused = True
        withheld += len(eligible)
        flags.append("prune-withheld-would-strand")
        for p in eligible:
            log_entries.append(
                reground_log_entry(
                    src_ref_id=p.src_ref_id,
                    src_chunk_id=p.src_chunk_id,
                    relation=p.relation,
                    verdict=p.verdict,
                    reason=p.reason,
                    action="withheld (would strand hub at zero edges)",
                    sha=plan.claim_sha,
                    handle=p.handle,
                )
            )
        eligible = []

    pruned = 0
    for p in eligible:
        try:
            pruned += remove_evidence(
                store,
                hub_ref_id=plan.hub_ref_id,
                src_ref_id=p.src_ref_id,
                src_chunk_id=p.src_chunk_id,
                role=p.relation,
                reason=p.reason,
                verdict=p.verdict,
                claim_sha=plan.claim_sha,
                handle=p.handle,
            )
        except WouldStrandHub:
            withheld += 1
            stranded_refused = True
            flags.append(f"prune-refused-last-edge:{p.src_ref_id}")
        except Exception:
            withheld += 1
            flags.append(f"prune-failed:{p.src_ref_id}")
            log.warning(
                "hub_refine: prune failed for hub #%d src #%d",
                plan.hub_ref_id,
                p.src_ref_id,
                exc_info=True,
            )

    with store.pool.connection() as conn:
        final = live_evidence_handles(conn, plan.hub_ref_id)
        if not final and plan.verdict != VERDICT_RETIRE:
            # Belt-and-braces: the simulation above and remove_evidence's
            # own guard should both have prevented this.
            flags.append("post-check-zero-edges")
        store.update_ref(
            plan.hub_ref_id,
            meta_patch={
                _META_REGROUND_INTENT: {
                    "sha": plan.claim_sha,
                    "at": datetime.now(UTC).isoformat(),
                    "handles": sorted(
                        [h.src_ref_id, h.src_chunk_id, h.relation]
                        for h in plan.intended_end_state()
                    ),
                }
            },
            conn=conn,
        )
        append_reground_log(store, plan.hub_ref_id, log_entries, conn=conn)
        conn.commit()

    return RegroundApplyResult(
        hub_ref_id=plan.hub_ref_id,
        confirmed_adds=len(confirmed),
        missing_adds=len(missing),
        pruned=pruned,
        withheld=withheld,
        contradicts_reattached=reattached,
        demoted=demoted,
        stranded_refused=stranded_refused,
        flags=tuple(flags),
    )


# ── intent-vs-committed: the first-class verify / repair mode ────────


@dataclass(frozen=True)
class RegroundDiff:
    """One hub's intent-vs-committed diff. ``missing_adds`` are edges the
    last applied plan intended that ``links`` does not hold;
    ``stale_edges`` are edges ``links`` holds that the plan intended to be
    gone. This is the technique that found the original 173020 damage —
    10 missing adds + 8 stale edges in one wave, none of which any error
    string mentioned — promoted from a one-off recovery script to a mode
    of the job."""

    hub_ref_id: int
    missing_adds: tuple[EvidenceHandle, ...] = ()
    stale_edges: tuple[EvidenceHandle, ...] = ()
    #: ``None`` when the hub carries no stored intent (never regrounded,
    #: or regrounded before this shipped) — distinct from "clean".
    has_intent: bool = True

    @property
    def clean(self) -> bool:
        return not (self.missing_adds or self.stale_edges)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hub": self.hub_ref_id,
            "has_intent": self.has_intent,
            "missing_adds": [
                [h.src_ref_id, h.src_chunk_id, h.relation] for h in self.missing_adds
            ],
            "stale_edges": [
                [h.src_ref_id, h.src_chunk_id, h.relation] for h in self.stale_edges
            ],
        }


def _stored_intent(conn: Connection, hub_ref_id: int) -> set[EvidenceHandle] | None:
    row = conn.execute(
        "SELECT meta FROM refs WHERE ref_id = %s AND retired_at IS NULL",
        (hub_ref_id,),
    ).fetchone()
    if row is None:
        return None
    raw = dict(row[0] or {}).get(_META_REGROUND_INTENT)
    if not isinstance(raw, dict):
        return None
    handles = raw.get("handles")
    if not isinstance(handles, list):
        return None
    out: set[EvidenceHandle] = set()
    for item in handles:
        if not isinstance(item, list) or len(item) != 3:
            continue
        src, chunk, rel = item
        if not isinstance(src, int) or not isinstance(rel, str):
            continue
        out.add(
            EvidenceHandle(
                src_ref_id=src,
                src_chunk_id=chunk if isinstance(chunk, int) else None,
                relation=rel,
            )
        )
    return out


def verify_hub_intent(store: Store, hub_ref_id: int) -> RegroundDiff:
    """Diff one hub's stored intended end state against ``links``. **No
    LLM spend, no writes** — safe to run over a whole draft's hub set as
    an audit."""
    with store.pool.connection() as conn:
        intent = _stored_intent(conn, hub_ref_id)
        if intent is None:
            return RegroundDiff(hub_ref_id=hub_ref_id, has_intent=False)
        committed = live_evidence_handles(conn, hub_ref_id)
    return RegroundDiff(
        hub_ref_id=hub_ref_id,
        missing_adds=tuple(sorted(intent - committed, key=_handle_sort_key)),
        stale_edges=tuple(sorted(committed - intent, key=_handle_sort_key)),
    )


def _handle_sort_key(h: EvidenceHandle) -> tuple[int, int, str]:
    return (
        h.src_ref_id,
        h.src_chunk_id if h.src_chunk_id is not None else -1,
        h.relation,
    )


def repair_hub_intent(
    store: Store, hub_ref_id: int, *, apply: bool = False, set_by: str = "system"
) -> RegroundDiff:
    """Apply a hub's intent-vs-committed delta, **adds first**.

    ``apply=False`` (default) is a dry run — identical to
    :func:`verify_hub_intent`. With ``apply=True``: re-issue every missing
    add, read the result back, and only then remove the stale edges,
    through the same :func:`~precis.taproot.hub.remove_evidence` door
    (which independently refuses a last-edge removal). Returns the
    *residual* diff — empty when the repair fully converged.
    """
    diff = verify_hub_intent(store, hub_ref_id)
    if not apply or diff.clean or not diff.has_intent:
        return diff

    kinds: dict[int, str] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, kind FROM refs WHERE ref_id = ANY(%s)",
            (sorted({h.src_ref_id for h in diff.missing_adds}),),
        ).fetchall()
        kinds = {int(r[0]): str(r[1]) for r in rows}

    for h in diff.missing_adds:
        try:
            attach_evidence(
                store,
                hub_ref_id=hub_ref_id,
                paper_ref_id=h.src_ref_id,
                role=h.relation,
                # No support: the intent memo stores only edge handles, so
                # the lost row's verdict (if it ever had one) is gone with
                # it — restoring membership must not fabricate one. The
                # re-attached edge is born withheld; the per-hub re-verify
                # arm (or the verify-edges sweep) re-certifies it.
                meta={
                    "source_handle": handle_registry.try_format(
                        kinds.get(h.src_ref_id, "paper"), h.src_chunk_id, chunk=True
                    ),
                    "reground": {"repair": True},
                },
                set_by=set_by,
                check_retraction=False,
            )
        except Exception:
            log.warning(
                "hub_refine: intent repair add failed for hub #%d src #%d",
                hub_ref_id,
                h.src_ref_id,
                exc_info=True,
            )

    # Read back before removing anything — the same never-trust-the-write
    # discipline apply_reground_plan uses.
    after_adds = verify_hub_intent(store, hub_ref_id)
    if after_adds.missing_adds:
        log.warning(
            "hub_refine: intent repair for hub #%d still missing %d add(s) — "
            "withholding stale-edge removal",
            hub_ref_id,
            len(after_adds.missing_adds),
        )
        return after_adds

    for h in after_adds.stale_edges:
        if h.relation not in HUB_ROLES:
            continue
        try:
            remove_evidence(
                store,
                hub_ref_id=hub_ref_id,
                src_ref_id=h.src_ref_id,
                src_chunk_id=h.src_chunk_id,
                role=h.relation,
                reason="intent-vs-committed repair: edge not in intended end state",
                verdict="STALE",
            )
        except Exception:
            log.warning(
                "hub_refine: intent repair prune failed for hub #%d src #%d",
                hub_ref_id,
                h.src_ref_id,
                exc_info=True,
            )
    return verify_hub_intent(store, hub_ref_id)


# ── the reground verify/write tail ──────────────────────────────────

#: Deeper passages offered per already-attached source. Small on purpose:
#: the top-ranked deeper passage is nearly always the primary (the pilot's
#: depth corrections were all rank-1 within the paper), and each extra
#: slot is one more strict-judge call multiplied by the hub's source
#: count. Two spares cover the case where rank 1 is itself front matter.
_PER_PAPER_K = 3


def _reground_verify_candidate(
    store: Store,
    conn: Connection,
    hub_ref_id: int,
    *,
    cand: _Candidate,
    claim_sentence: str,
    scope: dict[str, Any],
    depth_policy: str,
    cfg: RegroundConfig,
    seen: dict[str, Any],
    rejected: dict[str, Any],
    attached: set[int],
    plan: RegroundPlan,
    attached_this_pass: set[int],
    pending_checks: list[int] | None,
    pending_demotions: list[DemotionRequest] | None = None,
) -> None:
    """Judge ONE reground candidate with the strict judge and write the
    consequence. The reground counterpart of the Verify→Write tail above.

    Three verdicts, three writes:

    * ``KEEP`` → attach a ``corroborates`` edge, unless the
      grounding-depth policy refuses it (a measurement/mechanism claim
      will not accept a front-matter passage as its primary — that is the
      over-grounding the pilot caught the minter doing). The refusal
      applies only when the source actually **has** a deeper passage to
      prefer: a single-body-chunk source has no depth to correct to, and
      refusing there would drop a real supporter to gain nothing.
      Recorded in
      ``plan.adds`` so the applier can read it back and release the paired
      prune.
    * ``CONTRADICTS`` → files a plain, non-blocking ``disputes`` link
      (never a ``contradicts`` evidence edge — ADR 0073, repointed by
      docs/backlog/disputes-edge-nonblocking-disagreement.md D3; this
      candidate was never previously attached, so there is no prior
      evidence edge to convert, unlike :func:`apply_reground_plan`'s own
      contradicts->disputes conversion of an *already-attached*
      supporter). A primary-against passage is information, not noise.
    * ``PRUNE`` → memo. For a source we do NOT already hold an edge on,
      that memo is the ordinary ``taproot_rejected`` entry (which also
      excludes it from future discovery slots). For a source we DO hold an
      edge on, only the passage-grained ``reground_seen`` memo is written:
      marking the whole source rejected would be false — its other passage
      is still live evidence — and would evict a legitimate supporter from
      discovery.
    """
    block, ref = cand.block, cand.ref
    source_ref_id = cand.source_ref_id
    chunk_id = int(block.id)
    handle = handle_registry.try_format(ref.kind, chunk_id, chunk=True)
    # ``section_path`` is a first-class ``chunks`` column, popped out of
    # ``ChunkInsert.meta`` at insert time — so a ``ChunkRow`` read back from
    # ``search_chunks`` does NOT carry it, and the depth-policy filter has
    # to read it here rather than off ``block.meta``. The sibling count
    # rides along: the depth refusal only makes sense when this source has
    # somewhere deeper to go.
    row = conn.execute(
        "SELECT array_to_string(c.section_path, ' > '), "
        "(SELECT count(*) FROM chunks x WHERE x.ref_id = c.ref_id "
        "  AND x.ord >= 0 AND x.retired_at IS NULL) "
        "FROM chunks c WHERE c.chunk_id = %s",
        (chunk_id,),
    ).fetchone()
    section_path = str(row[0]) if row and row[0] else None
    n_body_chunks = int(row[1]) if row else 0
    verdict = cfg.judge_fn(
        claim=claim_sentence,
        scope=scope,
        cite_key=ref.slug or f"ref:{source_ref_id}",
        chunk_ord=block.ord,
        chunk_text=block.text,
        source_kind=ref.kind,
        depth_policy=depth_policy,
        section_path=section_path,
        neighbours=None,
    )
    if verdict is None:
        return  # no verdict — no memo, retried next pass
    seen[_seen_key(source_ref_id, chunk_id)] = {
        "sha": plan.claim_sha,
        "verdict": verdict.verdict,
        "at": datetime.now(UTC).isoformat(),
    }
    if verdict.verdict == "PRUNE":
        if source_ref_id not in attached:
            rejected[str(source_ref_id)] = {
                "at": datetime.now(UTC).isoformat(),
                "supports": "no",
                "contradicts": False,
                "via": "reground-judge",
            }
        return
    if (
        verdict.verdict == "KEEP"
        and depth_policy == DEPTH_BODY_REQUIRED
        and n_body_chunks > 1
    ):
        if is_front_matter(chunk_ord=block.ord, section_path=section_path):
            plan.log.append(
                reground_log_entry(
                    src_ref_id=source_ref_id,
                    src_chunk_id=chunk_id,
                    relation=_ROLE,
                    verdict="KEEP",
                    reason=(
                        "grounding-depth policy: measurement/mechanism claim "
                        "will not ground on a front-matter passage"
                    ),
                    action="withheld (depth policy)",
                    sha=plan.claim_sha,
                    handle=handle,
                )
            )
            return
    is_keep = verdict.verdict == "KEEP"
    edge_meta: dict[str, Any] = {
        "source_handle": handle,
        # Reversible as a set, the way the semantic backfill's
        # ``src_grounding.method`` marker is: every edge this stage
        # writes is queryable by ``meta->'reground'``.
        "reground": {
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "sha": plan.claim_sha,
            "via": cand.via,
        },
    }
    if is_keep:
        # A KEEP is a fresh strict-judge verification of this exact passage
        # — the edge is born verified (reason + fingerprint, never a bare
        # support stamp).
        edge_meta.update(
            {
                "support": "yes",
                "support_reason": verdict.reason,
                "caveats": [],
                **_verified_stamp(plan.claim_sha),
            }
        )
        attach_evidence(
            store,
            hub_ref_id=hub_ref_id,
            paper_ref_id=source_ref_id,
            role=_ROLE,
            meta=edge_meta,
            set_by="system",
            conn=conn,
            pending_checks=pending_checks,
        )
    else:
        # CONTRADICTS on a candidate never previously attached — file the
        # free, non-blocking ``disputes`` link, not an adjudicated
        # ``contradicts`` evidence edge (docs/backlog/disputes-edge-
        # nonblocking-disagreement.md D3). Same ``store.add_link`` +
        # ``validate_relation('disputes')`` shape
        # :func:`~precis.taproot.hub.reattach_as_disputes` uses for its own
        # add half; idempotent on the DB's endpoint+relation unique index.
        edge_meta.update({"support": "no", "caveats": []})
        validated_relation = validate_relation("disputes", store=store)
        store.add_link(
            src_ref_id=source_ref_id,
            dst_ref_id=hub_ref_id,
            relation=validated_relation,
            meta=edge_meta,
            set_by="system",
            conn=conn,
        )
    attached_this_pass.add(source_ref_id)
    if not is_keep and pending_demotions is not None:
        # Same consequence as apply_reground_plan's own contradicts-
        # >disputes conversion (and the enrichment arm's
        # ``_attach_disputes``): the hub's publish posture no longer
        # reflects its evidence the moment a primary-against passage
        # surfaces. Queued, not applied — this runs inside the caller's
        # transaction.
        pending_demotions.append(
            DemotionRequest(
                hub_ref_id=hub_ref_id,
                reason=(
                    f"reground judge attached a disputes edge from source "
                    f"#{source_ref_id}"
                ),
            )
        )
    if is_keep:
        plan.adds.append(
            RegroundAdd(
                src_ref_id=source_ref_id,
                src_chunk_id=chunk_id,
                handle=handle,
                via=cand.via,
            )
        )
    plan.log.append(
        reground_log_entry(
            src_ref_id=source_ref_id,
            src_chunk_id=chunk_id,
            relation=_ROLE if is_keep else "disputes",
            verdict=verdict.verdict,
            reason=verdict.reason,
            action="added",
            sha=plan.claim_sha,
            handle=handle,
        )
    )


def _hub_verdict(conn: Connection, hub_ref_id: int, plan: RegroundPlan) -> str:
    """The escalation ladder's read-out, computed — never a stored state.

    ``supportable`` when the hub will still hold at least one evidence
    edge once this plan's prunes land; otherwise ``retire`` if the
    external last resort actually ran and found nothing, else
    ``needs_external`` (the corpus could not substantiate it *yet*, and
    the ladder has a rung left). A hub that ends at zero edges reads
    ``unverified`` through ``.trust`` on its own — this string is a
    worklist label, not a trust flag.
    """
    live = live_evidence_handles(conn, hub_ref_id)
    doomed = {
        EvidenceHandle(p.src_ref_id, p.src_chunk_id, p.relation) for p in plan.prunes
    }
    if live - doomed:
        return VERDICT_SUPPORTABLE
    return VERDICT_RETIRE if plan.external_ran else VERDICT_NEEDS_EXTERNAL


# ── publish-gate re-verify of attached-but-unverified edges ──────────


def _reverify_pinned_edges(
    conn: Connection,
    store: Store,
    hub_ref_id: int,
    *,
    seen: dict[str, Any],
    sha: str,
) -> bool:
    """Certify this hub's attached-but-unverified evidence for the publish
    gate — the module docstring's re-verify-pinned-edges step.

    Two cohorts (``taproot.verify_edges``'s hub-scoped selectors):
    **withheld** (no ``support``, no ``publish_signoff``) and
    **unverified-stamped** (``support`` present but not trustworthy: no
    ``verified_by``, the mint-time default, or no ``verified_claim_sha``,
    a verdict from before that stamp existed). Each is re-read by the
    minter's own verifier
    (:func:`~precis.workers._chase_llm._verify_support_with_caveats`):

    * corroborating → stamped in the six-key shape
      (:func:`_verified_stamp`), dropping the edge from both cohorts
      (self-converges).
    * non-corroborating → NOT stamped and NOT stripped (stripping stays
      ``precis taproot verify-edges --unverified-stamped``'s door,
      pruning stays reground's) — memoed into ``seen`` (one judgment per
      edge per ``claim_sha``).
    * ``None`` (LLM failure), or a chunk with no live text → skipped, no
      memo, retried next pass.

    Capped at :data:`_REVERIFY_PER_PASS` calls/hub/pass. Returns True when
    ``seen`` gained an entry, so the caller persists the memo even when
    reground (its usual owner) is inactive.
    """
    from psycopg.types.json import Jsonb

    candidates = [
        *select_withheld_edges(store, hub_ref_id=hub_ref_id, limit=_REVERIFY_PER_PASS),
        *select_unverified_stamped_edges(
            store, hub_ref_id=hub_ref_id, limit=_REVERIFY_PER_PASS
        ),
    ]
    memoed = False
    spent = 0
    for edge in candidates:
        if spent >= _REVERIFY_PER_PASS:
            break
        key = _seen_key(edge.source_ref_id, edge.chunk_id)
        if seen.get(key, {}).get("sha") == sha:
            continue  # judged once per claim_sha (audit or an earlier pass)
        if not edge.chunk_text or edge.chunk_ord is None:
            continue  # pinned chunk retired/deleted — repair-evidence territory
        spent += 1
        verdict = _verify_support_with_caveats(
            claim=edge.sentence,
            scope=dict(edge.scope),
            target_cite_key=edge.cite_key or f"ref:{edge.source_ref_id}",
            target_chunk_ord=edge.chunk_ord,
            target_chunk_text=edge.chunk_text,
            source_kind=edge.source_kind,
        )
        if verdict is None:
            continue  # LLM failure — no memo, retried next pass
        if is_corroborating(verdict):
            patch = {
                "support": verdict.get("supports"),
                "support_reason": verdict.get("support_reason"),
                "caveats": list(verdict.get("caveats") or []),
                **_verified_stamp(sha),
            }
            conn.execute(
                "UPDATE links SET meta = COALESCE(meta, '{}'::jsonb) || %s "
                "WHERE link_id = %s",
                (Jsonb(patch), edge.link_id),
            )
            continue
        seen[key] = {
            "sha": sha,
            "verdict": "NO-CORROBORATION",
            "at": datetime.now(UTC).isoformat(),
            "via": "reverify",
        }
        memoed = True
    return memoed


def _refine_one_hub(
    conn: Connection,
    store: Store,
    hub_ref_id: int,
    *,
    embedder: Any,
    topk: int,
    min_sim: float | None,
    pending_checks: list[int] | None = None,
    pending_demotions: list[DemotionRequest] | None = None,
    reground: RegroundConfig | None = None,
    plan_out: list[RegroundPlan] | None = None,
) -> None:
    """Discover + verify + attach corroborators for one hub, then stamp it.

    Per-hub body of the module docstring's steps 2-6; see there for the
    two-source discover, filter, verify, write, and stamp contracts. This
    function's own specifics:

    **Vanished ref** — ``info is None`` (ref deleted between claim and
    processing): clears the attempt lease and returns; nothing to stamp.

    **Defensive compound skip** — :func:`_claim_hubs_due_for_refine` already
    excludes compound hubs from the due-set, but a concurrent decomposition
    can mint the ``conjunct-of`` edge in the claim→process window.
    :func:`_is_compound_hub` re-checks here: if true, stamps
    ``last_refined_at``/``last_refined_sha`` and clears the attempt lease,
    then returns before discovery/verify — ``attach_evidence``'s own
    compound guard is never reached.

    **Reground integration** (``reground`` non-``None``) — stages 1/2
    (fisheye + strict audit) run before discovery; stage 3 adds same-paper
    deeper passages ahead of the two discover sources and deepens their
    top-k; the ``attached`` pre-filter narrows to a memo-gated re-verify
    (:func:`_candidate_settled`); every candidate is judged by the strict
    judge, not the minter's verifier. Nothing is removed here: prunes
    accumulate into a :class:`RegroundPlan` appended to ``plan_out``,
    applied by :func:`apply_reground_plan` only after this function's
    transaction commits — that ordering is the add-first contract.
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

    # Reground state. ``reground_seen`` is the convergence guard and is
    # cleared by the same sha-reopen that clears the rejection memo.
    reground_seen: dict[str, Any] = (
        {} if reopened else dict(meta.get(_META_REGROUND_SEEN) or {})
    )
    plan: RegroundPlan | None = None
    depth_policy = DEPTH_ABSTRACT_OK
    attached_keys: set[str] = set()
    if reground is not None:
        depth_policy = claim_depth_policy(claim_sentence)
        attached_keys = _attached_edge_keys(conn, hub_ref_id)
        plan = RegroundPlan(
            hub_ref_id=hub_ref_id,
            claim_sha=new_sha,
            live_before=live_evidence_handles(conn, hub_ref_id),
        )
        _audit_edges(
            conn,
            hub_ref_id,
            claim_sentence=claim_sentence,
            scope=scope,
            depth_policy=depth_policy,
            cfg=reground,
            seen=reground_seen,
            plan=plan,
        )

    # Publish-gate re-verify (always on, embedder-independent): certify
    # this hub's attached-but-unverified evidence edges, which the
    # skip-if-attached precheck below can never reach. Runs AFTER the
    # reground audit so the strict judge wins the per-sha memo slot when
    # reground is active. Additive and bounded — see the function.
    reverify_memoed = False
    if claim_sentence:
        reverify_memoed = _reverify_pinned_edges(
            conn, store, hub_ref_id, seen=reground_seen, sha=new_sha
        )

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
        # Reground stage 3 deepens the recall floor: real primaries rank
        # low while the ``references`` categorization is still converging.
        effective_topk = topk if reground is None else reground.deeper_topk

        # Discover source 0 (reground only, offered FIRST): deeper
        # passages inside papers this hub already grounds on — the pilot's
        # dominant, lowest-risk fix ("right paper, wrong chunk").
        same_paper_cands: list[_Candidate] = []
        if reground is not None:
            same_paper_cands = _same_paper_candidates(
                store,
                hub_ref_id=hub_ref_id,
                source_refs=_attached_source_refs(conn, hub_ref_id),
                claim_sentence=claim_sentence,
                query_vec=query_vec,
                per_paper_k=_PER_PAPER_K,
            )

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
        # patent-evidence-parity.md). ``store.search_chunks``'s mode-
        # dispatched wrapper takes one ``kind=`` string, not a list, so this
        # is two calls merged by score (ascending cosine distance) and
        # truncated back to ``topk`` -- the bounded-spend guarantee doesn't
        # grow with the number of kinds feeding discovery. Patent
        # legal-claim blocks are dropped before the merge: legal scope is
        # not empirical support (grounding policy).
        #
        # The already-settled source refs (attached supporters + rejection
        # memo) are pushed INTO the query as ``exclude_ref_ids`` rather than
        # left to the precheck below. The precheck alone is correct but
        # wasteful: a settled source still occupies one of the ``topk``
        # slots, so a hub whose nearest neighbours are all sources it
        # already has widens by nothing — the discovery budget is spent
        # re-offering evidence it holds. Filtering in SQL hands those slots
        # to genuinely new sources. Exclusion is **ref**-grained, matching
        # this module's source-level dedup (see :func:`_attached_source_ids`):
        # a different passage from a settled source is excluded too, because
        # the precheck would skip it anyway. The precheck stays as the
        # authority — the citation leg is deliberately unfiltered, and
        # verdicts land mid-loop.
        settled_ref_ids = sorted(attached | _rejected_source_ids(rejected))
        paper_hits = store.chunks.search_chunks(
            q=claim_sentence,
            query_vec=query_vec,
            mode="semantic",
            kind="paper",
            limit=effective_topk,
            max_distance=min_sim,
            exclude_ref_ids=settled_ref_ids,
        )
        patent_hits = _drop_patent_claim_blocks(
            store.chunks.search_chunks(
                q=claim_sentence,
                query_vec=query_vec,
                mode="semantic",
                kind="patent",
                limit=effective_topk,
                max_distance=min_sim,
                exclude_ref_ids=settled_ref_ids,
            )
        )
        sem_hits = sorted([*paper_hits, *patent_hits], key=lambda hit: hit[2])[
            :effective_topk
        ]
        sem_cands = [
            _Candidate(block=block, ref=ref, via="semantic")
            for block, ref, _score in sem_hits
        ]

        # Stage 5 (external last resort) feeds the SAME candidate tail —
        # it is a discover source, not a second pipeline. "Last resort"
        # means the corpus legs offered nothing *new*: a leg that came
        # back full of passages this hub already grounds on (or already
        # judged at this sha) has substantiated nothing, so the count of
        # UNSETTLED candidates is the trigger, not the raw hit count.
        corpus_cands = [*same_paper_cands, *cite_cands, *sem_cands]
        ext_cands: list[_Candidate] = []
        corpus_offered_something_new = any(
            not _candidate_settled(
                source_ref_id=c.source_ref_id,
                chunk_id=int(c.block.id),
                attached=attached,
                attached_keys=attached_keys,
                rejected=rejected,
                reground=reground,
                seen=reground_seen,
                sha=new_sha,
            )
            for c in corpus_cands
            if c.source_ref_id != hub_ref_id
        )
        if (
            reground is not None
            and reground.external
            and not corpus_offered_something_new
        ):
            ext_cands, ext_report = _external_candidates(
                conn,
                store,
                hub_ref_id,
                claim_sentence=claim_sentence,
                query_vec=query_vec,
                grounding_ref_ids=[
                    r for r, _ in _attached_source_refs(conn, hub_ref_id)
                ],
                probe_fn=reground.external_probe_fn,
            )
            store.update_ref(
                hub_ref_id, meta_patch={_META_REGROUND_EXTERNAL: ext_report}, conn=conn
            )
            if plan is not None:
                plan.external_ran = True

        # Shared dedup slot. Additive-only enrichment dedups at the
        # SOURCE-ref level (a supporter is a paper, not a passage — see
        # _attached_source_ids). Reground has to dedup at the PASSAGE
        # level, because its primary move is a second passage of a source
        # it already holds; the per-source attach cap below restores the
        # bound that source-level dedup used to provide.
        seen_slots: set[str] = set()
        attached_this_pass: set[int] = set()
        for cand in [*corpus_cands, *ext_cands]:
            source_ref_id = cand.source_ref_id
            if source_ref_id == hub_ref_id:
                continue
            block, ref = cand.block, cand.ref
            chunk_id = int(block.id) if reground is not None else None
            slot = (
                str(source_ref_id)
                if reground is None
                else _seen_key(source_ref_id, chunk_id)
            )
            if slot in seen_slots:
                continue
            seen_slots.add(slot)
            if source_ref_id in attached_this_pass:
                # One attach per source per pass: the deeper-passage leg
                # must not turn one paper into a fan of near-duplicate
                # edges, and the bounded-spend guarantee is per hub.
                continue
            # Precheck BEFORE verify (idempotency + rejection memo): a
            # source ref already an evidence supporter of this hub (any
            # role, any grounding chunk) or already judged ``no`` must never
            # cost another LLM call. Reground narrows the ``attached`` half
            # of that to a memo-gated re-verify — see _candidate_settled.
            if _candidate_settled(
                source_ref_id=source_ref_id,
                chunk_id=chunk_id,
                attached=attached,
                attached_keys=attached_keys,
                rejected=rejected,
                reground=reground,
                seen=reground_seen,
                sha=new_sha,
            ):
                continue
            if reground is not None and plan is not None:
                _reground_verify_candidate(
                    store,
                    conn,
                    hub_ref_id,
                    cand=cand,
                    claim_sentence=claim_sentence,
                    scope=scope,
                    depth_policy=depth_policy,
                    cfg=reground,
                    seen=reground_seen,
                    rejected=rejected,
                    attached=attached,
                    plan=plan,
                    attached_this_pass=attached_this_pass,
                    pending_checks=pending_checks,
                    pending_demotions=pending_demotions,
                )
                continue
            verification = _verify_support_with_caveats(
                claim=claim_sentence,
                scope=scope,
                target_cite_key=ref.slug or f"ref:{source_ref_id}",
                target_chunk_ord=block.ord,
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
                    # A verification just ran against this exact passage, so
                    # the edge is born verified: reason + fingerprint ride
                    # with the verdict (support alone is never written).
                    meta={
                        "support": supports,
                        "support_reason": verification.get("support_reason"),
                        "caveats": list(verification.get("caveats") or []),
                        "source_handle": handle_registry.try_format(
                            ref.kind, block.id, chunk=True
                        ),
                        **_verified_stamp(new_sha),
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
                if contradicts:
                    # A primary-against passage is information, not noise —
                    # the same call ADR 0073 makes on the reground path
                    # (:func:`_reground_verify_candidate`). Until 2026-08 the
                    # enrichment arm discarded it: the verdict went into the
                    # memo and the edge was never written, so the one
                    # automated pass that could find contradicting evidence
                    # threw the finding on the floor and the hub kept
                    # ratcheting up. The memo entry above STILL lands (a
                    # contradicting source must never re-enter discovery and
                    # re-spend the verifier) — the edge is additional, not a
                    # replacement.
                    _attach_disputes(
                        store,
                        conn,
                        hub_ref_id=hub_ref_id,
                        source_ref_id=source_ref_id,
                        handle=handle_registry.try_format(
                            ref.kind, block.id, chunk=True
                        ),
                        reason=verification.get("support_reason"),
                        caveats=list(verification.get("caveats") or []),
                        sha=new_sha,
                        via=cand.via,
                        pending_demotions=pending_demotions,
                    )
                    # No ``attached_this_pass`` bookkeeping here: this arm
                    # only runs with ``reground is None``, where ``slot`` is
                    # the bare source ref id, so ``seen_slots`` already caps
                    # a source at one visit per pass. The per-source attach
                    # cap exists for reground's passage-level dedup.
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
    if reverify_memoed and reground is None:
        # The re-verify arm's non-corroborating memos must persist even
        # without reground active (the memo's usual writer) — they are what
        # stops a non-corroborating edge from re-spending the verifier
        # every pass.
        meta_patch[_META_REGROUND_SEEN] = reground_seen
    if reground is not None and plan is not None:
        meta_patch[_META_REGROUND_SEEN] = reground_seen
        plan.verdict = _hub_verdict(conn, hub_ref_id, plan)
        if plan.verdict == VERDICT_RETIRE and _retire_authorized(
            conn, hub_ref_id, reground
        ):
            _flag_retire(conn, store, hub_ref_id, plan)
        elif plan.verdict != VERDICT_SUPPORTABLE:
            meta_patch[_META_REGROUND_VERDICT] = {
                "verdict": plan.verdict,
                "sha": new_sha,
                "at": datetime.now(UTC).isoformat(),
            }
        if plan_out is not None:
            plan_out.append(plan)
    store.update_ref(hub_ref_id, meta_patch=meta_patch, conn=conn)
    # This point is only reached on a completed run (a raise anywhere above
    # propagates out and this line never runs) -- clear the claim-time
    # attempt lease so a genuine re-trigger is never blocked by a stale one.
    store.remove_tag(hub_ref_id, Tag.closed(_ATTEMPT_NS, _ATTEMPT_VALUE), conn=conn)


# ── runner ─────────────────────────────────────────────────────────


def reground_one_hub(
    store: Store,
    hub_ref_id: int,
    *,
    embedder: Any,
    cfg: RegroundConfig,
    topk: int | None = None,
    min_sim: float | None = None,
) -> RegroundApplyResult:
    """Reground exactly ONE hub, end to end — the ``reground_claim`` job's
    per-hub unit, and the ``hub_ids``-override path that bypasses the
    due-set entirely so a draft's whole hub set can be regrounded on
    demand.

    Same two-phase shape :func:`run_hub_refine_pass` uses, and for the
    same reason: :func:`_refine_one_hub` (discover → strict judge →
    attach → stamp) runs in one transaction and commits; only then does
    :func:`apply_reground_plan` read the adds back and release the paired
    prunes. Raises whatever the refine phase raises — the caller isolates
    per hub.
    """
    resolved_topk = topk if topk is not None else _topk_default()
    resolved_min_sim = min_sim if min_sim is not None else _min_sim_default()
    pending_checks: list[int] = []
    pending_demotions: list[DemotionRequest] = []
    plans: list[RegroundPlan] = []
    with store.pool.connection() as conn:
        _refine_one_hub(
            conn,
            store,
            hub_ref_id,
            embedder=embedder,
            topk=resolved_topk,
            min_sim=resolved_min_sim,
            pending_checks=pending_checks,
            pending_demotions=pending_demotions,
            reground=cfg,
            plan_out=plans,
        )
        conn.commit()
    run_retraction_checks(store, pending_checks, hub_ref_id=hub_ref_id)
    run_demotions(store, pending_demotions)
    if not plans:
        # The hub drained without a plan (vanished, or became compound
        # between claim and processing) — nothing was judged, nothing to
        # apply.
        return RegroundApplyResult(hub_ref_id=hub_ref_id)
    return apply_reground_plan(store, plans[0])


def run_hub_refine_pass(
    store: Store,
    *,
    limit: int | None = None,
    embedder: Any | None = None,
    topk: int | None = None,
    min_sim: float | None = None,
    reground: RegroundConfig | None = None,
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
    ``store.search_chunks``'s internal lexical fallback, which would
    quietly turn "ANN over paper chunks" into a much weaker keyword
    match without ever telling the operator.

    ``reground`` defaults to :meth:`RegroundConfig.from_env`, which is
    ``None`` unless an operator has opted in — so the service's behaviour
    is byte-identical to additive-only enrichment until the flags are set
    (and the prune sub-stage needs its own second flag on top, behind the
    ``slice_refine_eval`` rubric gate). Tests pass it explicitly.

    Returns the standard ``{claimed, ok, failed}`` shape, plus reground
    counters (``pruned``/``withheld``) when reground ran at all.
    """
    if embedder is None:
        log.warning("hub_refine: no embedder available -- pass degrades to a no-op")
        return {"claimed": 0, "ok": 0, "failed": 0}

    resolved_limit = limit if limit is not None else _hubs_per_pass()
    resolved_topk = topk if topk is not None else _topk_default()
    resolved_backstop_h = _backstop_hours()
    resolved_min_sim = min_sim if min_sim is not None else _min_sim_default()
    resolved_reground = reground if reground is not None else RegroundConfig.from_env()

    with store.pool.connection() as conn:
        hub_ids = _claim_hubs_due_for_refine(
            conn, store, limit=resolved_limit, backstop_h=resolved_backstop_h
        )
        conn.commit()

    claimed = len(hub_ids)
    ok = 0
    failed = 0
    pruned = 0
    withheld = 0
    demoted = 0

    for hub_ref_id in hub_ids:
        try:
            # Trigger-1 checks do Crossref HTTP and open their own
            # connections, so they are collected during the write and run
            # only after the commit below — never inside the transaction
            # (see ``taproot.hub.attach_evidence``).
            pending_checks: list[int] = []
            pending_demotions: list[DemotionRequest] = []
            plans: list[RegroundPlan] = []
            with store.pool.connection() as conn:
                _refine_one_hub(
                    conn,
                    store,
                    hub_ref_id,
                    embedder=embedder,
                    topk=resolved_topk,
                    min_sim=resolved_min_sim,
                    pending_checks=pending_checks,
                    pending_demotions=pending_demotions,
                    reground=resolved_reground,
                    plan_out=plans,
                )
                conn.commit()
            run_retraction_checks(store, pending_checks, hub_ref_id=hub_ref_id)
            # The publish posture follows the evidence: a hub that gained a
            # contradicts edge above walks back down the freeze ladder (or,
            # if its bytes are frozen, raises for a human).
            demoted += sum(
                1 for d in run_demotions(store, pending_demotions) if d.applied
            )
            # Prunes run only now — after the adds above are committed and
            # can be read back (docs/backlog/taproot-reground.md's
            # add-first contract, enforced in code).
            for plan in plans:
                applied = apply_reground_plan(store, plan)
                pruned += applied.pruned
                withheld += applied.withheld
                demoted += applied.demoted
            ok += 1
        except Exception:  # pragma: no cover — defensive, mirrors inbound_chase.py
            log.warning(
                "hub_refine: refine failed for hub #%d", hub_ref_id, exc_info=True
            )
            failed += 1

    result = {"claimed": claimed, "ok": ok, "failed": failed}
    if demoted:
        result["demoted"] = demoted
    if resolved_reground is not None:
        result["pruned"] = pruned
        result["withheld"] = withheld
    return result


__all__ = [
    "DEPTH_ABSTRACT_OK",
    "DEPTH_BODY_REQUIRED",
    "PRUNE_INTERLOCK_TOKEN",
    "STRICT_VERDICTS",
    "VERDICT_NEEDS_EXTERNAL",
    "VERDICT_RETIRE",
    "VERDICT_SUPPORTABLE",
    "RegroundAdd",
    "RegroundApplyResult",
    "RegroundConfig",
    "RegroundContradict",
    "RegroundDiff",
    "RegroundPlan",
    "RegroundPrune",
    "StrictVerdict",
    "apply_reground_plan",
    "claim_depth_policy",
    "is_front_matter",
    "judge_edge_strict",
    "prune_interlock_open",
    "reground_one_hub",
    "repair_hub_intent",
    "run_hub_refine_pass",
    "verify_hub_intent",
]
