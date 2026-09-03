"""``conflict_search`` — every claim hub hunts its own opposition.

Slice 1 of ``docs/backlog/claim-conflict-search.md`` (items 1-3: search,
budgeted verify, coverage ledger; items 4-5, the approve-time advisory
panel and the counter-claim mint, are later slices). A standing,
watermarked ref-pass — mirrors ``hub_tagline``'s claim-and-lease shape,
not ``hub_refine``'s single-transaction discover→verify→write spine
(this pass is simpler: no compound-hub handling, no rejection memo, no
reground extension).

**One mechanism, two populations.** The cohort is every live claim hub
(:func:`~precis.taproot.canon.claim_hub_predicate_sql`, re-derived
``not_hypothesis_predicate_sql`` — a hypothesis is a confirmation target,
not a conflict-search target) whose ``meta.conflict_search.version`` is
missing or older than :data:`CONFLICT_SEARCH_VERSION`. That single
watermark rule makes "sweep a freshly-minted hub" and "backfill the
existing corpus" the same code path.

Per hub:

1. **Negate** (:func:`negate_claim`, MEDIUM tier) — 1-3 LLM-generated
   sentences asserting the OPPOSITE or a conflicting version of the
   claim. "X has no effect on Y" sits far from "X enhances Y" in
   embedding space, so searching only the claim's own phrasing is
   structurally blind to the disagreements most worth finding
   (docs/backlog/claim-conflict-search.md's decisions log). A dispatch
   failure skips the hub *without* stamping the watermark — the lease is
   cleared instead, so a transient LLM outage is retried next pass
   rather than parked behind the TTL.
2. **Search** — ANN over paper/patent/finding body chunks for the claim
   sentence *and* every paraphrase, deduped by chunk id (best distance
   wins), excluding the hub itself and every ref already joined to it by
   a live evidence-shaped link (:data:`_EXCLUDE_RELATIONS`:
   ``taproot.hub.HUB_ROLES`` plus ``disputes`` — already-adjudicated
   opposition needs no re-finding).
3. **Rank + floor** — candidates are ordered by
   ``meta.paper_rank.read_first`` (the existing reading-priority score,
   ``workers/paper_rank.py`` — consumed as-is, never re-derived) into a
   high band (>= median) and a low band (< median, plus every
   unranked candidate), verified with :data:`_FLOOR_FRACTION` of the
   budget reserved for the low band whenever it's non-empty (tuning
   starts here; the real number comes from the first dense-neighbourhood
   backfill). Nothing is dropped on rank alone — only the budget bounds
   spend, so dissent that disproportionately lives in low-prestige
   venues still gets a verify slot.
4. **Verify** — the sanctioned shared seam,
   ``workers._chase_llm._verify_support_with_caveats`` (never forked;
   docs/backlog/claim-conflict-search.md's "Boundary with hub_refine").
   A dispatch failure consumes the budget slot and counts toward
   ``llm_errors`` — never an edge.
5. **File** — a confirmed ``contradicts`` verdict on a candidate never
   previously attached to this hub becomes a plain, non-blocking
   ``disputes`` link (Part 1 of
   docs/backlog/disputes-edge-nonblocking-disagreement.md shipped +
   deployed 2026-09-03, so slice 1 files directly rather than parking
   verdicts). Idempotent on ``links``' endpoint+relation unique index —
   a re-sweep never duplicates it.
6. **Stamp** — ``meta.conflict_search = {version, at,
   candidates_checked, disputes_filed}`` written unconditionally on a
   completed sweep (even a hub with zero candidates: "no known conflict
   as of <date>" is a checkable statement, not silence), and the claim
   lease cleared. Coverage (swept/total at the live version) is then one
   query.

Re-derives its own discovery wiring rather than importing
``hub_refine``'s underscore-private helpers (a deliberate spec decision,
docs/backlog/claim-conflict-search.md item 1's boundary note) — the only
shared import is ``workers/_chase_llm.py``'s verifier.

Registered as the ``conflict_search`` :class:`~precis.workers.registry.
ServiceSpec` (dark, like every other taproot service); wired in
``cli/worker.py``'s ``_register``.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from precis.errors import NotFound
from precis.handlers._link_tag_ops import validate_relation
from precis.store import Store
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    claim_hub_predicate_sql,
    not_hypothesis_predicate_sql,
)
from precis.taproot.hub import HUB_ROLES
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.llm.router import LlmRequest, Tier, route
from precis.workers._chase_llm import _verify_support_with_caveats

log = logging.getLogger(__name__)

__all__ = [
    "CONFLICT_SEARCH_VERSION",
    "NegateFn",
    "VerifyFn",
    "negate_claim",
    "run_conflict_search_pass",
]

#: Bump to re-sweep every hub (the watermark rule, module docstring).
CONFLICT_SEARCH_VERSION = 1

#: Default hubs claimed per pass invocation — mirrors
#: ``hub_refine.py::_hubs_per_pass``'s env-int shape.
_DEFAULT_HUBS_PER_PASS = 4

#: TTL (minutes) on the claim-and-lease ``meta.conflict_search_claimed_at``
#: stamp — mirrors ``hub_tagline``'s ``_CLAIM_TTL_MIN``: a crashed
#: mid-batch pass doesn't strand a hub forever.
_CLAIM_TTL_MIN = 10

#: ANN candidates requested per (query, kind) leg.
_DEFAULT_TOPK = 8

#: LLM-verify calls spent per hub per pass.
_DEFAULT_VERIFY_BUDGET = 6

#: Fraction of a hub's verify budget reserved for below-median-``read_first``
#: candidates (the small-voice floor, docs/backlog/claim-conflict-search.md
#: item 2) — tuning happens on the first dense-neighbourhood backfill, not
#: here.
_FLOOR_FRACTION = 0.2

#: A ref already joined to the hub by one of these relations (either
#: direction) is excluded from discovery — it's already evidence-shaped
#: (an evidence role) or already-adjudicated opposition
#: (``disputes``), so re-finding it spends budget for nothing.
_EXCLUDE_RELATIONS: tuple[str, ...] = tuple(sorted(HUB_ROLES | {"disputes"}))

#: Chunk kinds searched per query string.
_SEARCH_KINDS: tuple[str, ...] = ("paper", "patent", "finding")


def _hubs_per_pass() -> int:
    try:
        return int(
            os.environ.get(
                "PRECIS_CONFLICT_SEARCH_HUBS_PER_PASS", str(_DEFAULT_HUBS_PER_PASS)
            )
        )
    except ValueError:
        return _DEFAULT_HUBS_PER_PASS


def _topk() -> int:
    try:
        return int(os.environ.get("PRECIS_CONFLICT_SEARCH_TOPK", str(_DEFAULT_TOPK)))
    except ValueError:
        return _DEFAULT_TOPK


def _verify_budget() -> int:
    try:
        return int(
            os.environ.get(
                "PRECIS_CONFLICT_SEARCH_VERIFY_BUDGET", str(_DEFAULT_VERIFY_BUDGET)
            )
        )
    except ValueError:
        return _DEFAULT_VERIFY_BUDGET


# ── cohort + claim-and-lease ────────────────────────────────────────────

#: Live claim hubs, not a hypothesis, whose conflict-search coverage is
#: missing or stale, not currently leased by another node's in-flight
#: sweep. Mirrors ``hub_tagline.py``'s ``_COHORT_SQL`` shape.
_COHORT_SQL = f"""\
    SELECT r.ref_id, r.title, r.meta
      FROM refs r
     WHERE r.kind = 'finding'
       AND r.retired_at IS NULL
       AND {claim_hub_predicate_sql()}
       AND {not_hypothesis_predicate_sql()}
       AND (
             r.meta->'conflict_search'->>'version' IS NULL
             OR (r.meta->'conflict_search'->>'version')::int < %(version)s
           )
       AND (r.meta->>'conflict_search_claimed_at' IS NULL
            OR (r.meta->>'conflict_search_claimed_at')::timestamptz
                 < now() - make_interval(mins => %(ttl_min)s))
     ORDER BY r.ref_id
     LIMIT %(limit)s
       FOR UPDATE OF r SKIP LOCKED
"""


def _claim_hubs(store: Store, *, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    """Atomically claim up to ``limit`` due hubs: ``(ref_id, title, meta)``.

    Same ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) ...
    RETURNING`` idiom as ``hub_tagline._claim_candidates`` — stamps
    ``meta.conflict_search_claimed_at`` atomically so two racing nodes
    never both pay for the same hub's sweep within the lease TTL.
    """
    if limit <= 0:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            UPDATE refs r
               SET meta = r.meta || jsonb_build_object(
                             'conflict_search_claimed_at', now()::text)
              FROM ({_COHORT_SQL}) c
             WHERE r.ref_id = c.ref_id
             RETURNING r.ref_id, c.title, c.meta
            """,
            {
                **CLAIM_HUB_PREDICATE_PARAMS,
                **NOT_HYPOTHESIS_PREDICATE_PARAMS,
                "version": CONFLICT_SEARCH_VERSION,
                "ttl_min": _CLAIM_TTL_MIN,
                "limit": limit,
            },
        ).fetchall()
    claimed = [(int(r[0]), str(r[1] or ""), dict(r[2] or {})) for r in rows]
    claimed.sort(key=lambda c: c[0])
    return claimed


# ── negate — MEDIUM, 1-3 opposing paraphrases ───────────────────────────

_PROMPT_NEGATE = """\
You are generating NEGATED / OPPOSING paraphrases of a scientific claim, so
they can be searched for in a corpus to surface sources that disagree with it.

CLAIM SENTENCE:
{sentence}

SCOPE (structured context; may be empty):
{scope_json}

Write 1 to 3 short, self-contained, plain-text sentences (no TeX, no bullet
markers), each asserting either:
  - the OPPOSITE of the claim, or
  - a conflicting version of it (a different value, mechanism, or tendency
    under the same conditions).

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "paraphrases": ["<opposing sentence 1>", ...]
}}
"""

#: The injectable negate seam — ``(sentence, scope)`` to the parsed JSON
#: dict, or ``None`` on dispatch failure (the ``_chase_llm`` / ``reword``
#: contract every LLM hook in this codebase shares).
NegateFn = Callable[[str, dict[str, Any]], "dict[str, Any] | None"]


def negate_claim(sentence: str, scope: dict[str, Any]) -> dict[str, Any] | None:
    """One MEDIUM-tier negated-paraphrase proposal. Returns the parsed JSON
    dict, or ``None`` on dispatch failure — the caller treats that as
    transient and retries the hub next pass without stamping the
    watermark."""
    prompt = _PROMPT_NEGATE.format(
        sentence=sentence, scope_json=json.dumps(scope, sort_keys=True)
    )
    res = route(
        LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="conflict_search:negate")
    )
    if res.error:
        log.warning("conflict_search: negate hook failed: %s", res.error)
        return None
    return res.data


def _parse_paraphrases(data: dict[str, Any] | None) -> list[str]:
    """Up to 3 non-empty string paraphrases from a (possibly malformed)
    negate reply. Never raises — a bad shape degrades to an empty list,
    which just means the sweep searches the claim sentence alone."""
    if not isinstance(data, dict):
        return []
    raw = data.get("paraphrases")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= 3:
            break
    return out


# ── discover — ANN over claim + paraphrases, deduped ────────────────────


@dataclass
class _Candidate:
    """One discover-step candidate passage, deduped by ``chunk_id`` (best
    distance wins across every query string it was found under)."""

    chunk_id: int
    chunk_ord: int
    chunk_text: str
    ref_id: int
    ref_kind: str
    distance: float
    found_by: list[str] = field(default_factory=list)
    #: ``meta.paper_rank.read_first`` for ``ref_id``, or ``None`` if the
    #: source has never been ranked. Filled by :func:`_attach_read_first`.
    read_first: float | None = None


def _excluded_ref_ids(store: Store, hub_ref_id: int) -> list[int]:
    """The hub itself, plus every ref already joined to it (either
    direction) by one of :data:`_EXCLUDE_RELATIONS` — already evidence-
    shaped or already-adjudicated opposition, so it never occupies a
    discovery slot."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT CASE WHEN src_ref_id = %(hub)s THEN dst_ref_id
                                  ELSE src_ref_id END AS other_ref_id
              FROM links
             WHERE (src_ref_id = %(hub)s OR dst_ref_id = %(hub)s)
               AND relation = ANY(%(relations)s)
            """,
            {"hub": hub_ref_id, "relations": list(_EXCLUDE_RELATIONS)},
        ).fetchall()
    return [hub_ref_id] + [int(r[0]) for r in rows]


def _discover(
    store: Store,
    embedder: Any,
    *,
    hub_ref_id: int,
    claim_sentence: str,
    paraphrases: list[str],
    topk: int,
) -> list[_Candidate]:
    """ANN over paper/patent/finding body chunks for the claim sentence
    *and* every paraphrase, deduped by ``chunk_id`` (smallest cosine
    distance wins; ``found_by`` records every query label that surfaced
    it — ``"claim"`` or ``"paraphrase:<i>"``, which is what pins the
    negated-paraphrase-extends-retrieval acceptance test)."""
    excluded = _excluded_ref_ids(store, hub_ref_id)
    queries: list[tuple[str, str]] = [("claim", claim_sentence)]
    queries.extend((f"paraphrase:{i}", p) for i, p in enumerate(paraphrases))

    by_chunk: dict[int, _Candidate] = {}
    for label, q in queries:
        query_vec = embed_query(embedder, q)
        if query_vec is None:
            continue
        for kind in _SEARCH_KINDS:
            hits = store.chunks.search_chunks(
                q=q,
                query_vec=query_vec,
                mode="semantic",
                kind=kind,
                limit=topk,
                exclude_ref_ids=excluded,
            )
            for block, ref, distance in hits:
                chunk_id = int(block.id)
                dist = float(distance)
                existing = by_chunk.get(chunk_id)
                if existing is None:
                    by_chunk[chunk_id] = _Candidate(
                        chunk_id=chunk_id,
                        chunk_ord=int(block.ord),
                        chunk_text=str(block.text),
                        ref_id=int(ref.id),
                        ref_kind=str(ref.kind),
                        distance=dist,
                        found_by=[label],
                    )
                else:
                    if dist < existing.distance:
                        existing.distance = dist
                    if label not in existing.found_by:
                        existing.found_by.append(label)
    return list(by_chunk.values())


#: Which ``ref_id``s among a candidate pool's ``'finding'``-kind entries
#: are live canonical claim hubs — the finding-kind ANN leg surfaces every
#: embedded ``finding_body`` chunk, not just canonical hubs (a chase-tree
#: scratch node, a ``dead_chain`` finding, or a hypothesis all carry one
#: too). Re-derives :func:`~precis.taproot.canon.claim_hub_predicate_sql`
#: rather than trusting the source pass's own claim-hub-ness (the same
#: divergence bug ``claim_hub_predicate_sql``'s docstring warns about: "three
#: readers once didn't [re-derive the predicate] and offered 280 chase
#: findings as hubs").
_FINDING_CANDIDATE_SQL = f"""\
    SELECT r.ref_id
      FROM refs r
     WHERE r.ref_id = ANY(%(ref_ids)s)
       AND r.kind = 'finding'
       AND r.retired_at IS NULL
       AND {claim_hub_predicate_sql()}
       AND {not_hypothesis_predicate_sql()}
"""


def _filter_finding_candidates(
    store: Store, candidates: list[_Candidate]
) -> list[_Candidate]:
    """Drop ``'finding'``-kind candidates that are not live canonical claim
    hubs. Paper/patent candidates pass through untouched — the predicate
    only applies to the ``'finding'`` kind, which is the only one the
    corpus-wide ANN leg can surface off-lifecycle rows for."""
    finding_ref_ids = sorted({c.ref_id for c in candidates if c.ref_kind == "finding"})
    if not finding_ref_ids:
        return candidates
    with store.pool.connection() as conn:
        rows = conn.execute(
            _FINDING_CANDIDATE_SQL,
            {
                "ref_ids": finding_ref_ids,
                **CLAIM_HUB_PREDICATE_PARAMS,
                **NOT_HYPOTHESIS_PREDICATE_PARAMS,
            },
        ).fetchall()
    live_hub_ids = {int(r[0]) for r in rows}
    return [
        c for c in candidates if c.ref_kind != "finding" or c.ref_id in live_hub_ids
    ]


def _attach_read_first(store: Store, candidates: list[_Candidate]) -> None:
    """Fill in each candidate's ``read_first`` from
    ``refs.meta.paper_rank.read_first`` — one batched query, never a
    per-candidate round trip."""
    if not candidates:
        return
    ref_ids = sorted({c.ref_id for c in candidates})
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, (meta->'paper_rank'->>'read_first')::float "
            "FROM refs WHERE ref_id = ANY(%s)",
            (ref_ids,),
        ).fetchall()
    read_first = {int(r[0]): (float(r[1]) if r[1] is not None else None) for r in rows}
    for c in candidates:
        c.read_first = read_first.get(c.ref_id)


# ── rank + floor ─────────────────────────────────────────────────────────


def _select_for_verify(candidates: list[_Candidate], budget: int) -> list[_Candidate]:
    """Which candidates get an LLM-verify slot this hub, out of ``budget``.

    High band (``read_first`` >= the pool's median among *ranked*
    candidates) ordered by ``read_first`` descending; low band (below
    median, plus every unranked candidate) ordered by cosine distance
    ascending. :data:`_FLOOR_FRACTION` of the budget is reserved for the
    low band whenever it's non-empty; an under-filled reservation (or an
    exhausted high band) spills into the other band rather than going
    unspent. Never drops a candidate outright — only the budget bounds
    how many get verified.
    """
    if budget <= 0 or not candidates:
        return []
    ranked = [c for c in candidates if c.read_first is not None]
    unranked = [c for c in candidates if c.read_first is None]
    if ranked:
        ranked_scores = [c.read_first for c in ranked if c.read_first is not None]
        median = statistics.median(ranked_scores)
        low_ranked = [
            c for c in ranked if c.read_first is not None and c.read_first < median
        ]
        high_ranked = [
            c for c in ranked if c.read_first is not None and c.read_first >= median
        ]
    else:
        low_ranked, high_ranked = [], []

    low = sorted(low_ranked + unranked, key=lambda c: c.distance)
    high = sorted(high_ranked, key=lambda c: c.read_first or 0.0, reverse=True)

    reserved = max(1, round(_FLOOR_FRACTION * budget)) if low else 0
    reserved = min(reserved, budget)
    low_take = min(reserved, len(low))
    selected = list(low[:low_take])

    remaining = budget - len(selected)
    high_take = min(remaining, len(high))
    selected.extend(high[:high_take])

    remaining = budget - len(selected)
    if remaining > 0:
        selected.extend(low[low_take : low_take + remaining])

    return selected


# ── verify + file ────────────────────────────────────────────────────────

#: The injectable verify seam's shape — the same ``_chase_llm``/
#: ``hub_refine`` verify contract (keyword-only, parsed JSON dict or
#: ``None`` on dispatch failure).
VerifyFn = Callable[..., "dict[str, Any] | None"]


@dataclass(frozen=True)
class _SweepResult:
    swept: bool
    checked: int
    disputes_filed: int
    llm_errors: int
    vanished: bool = False


def _sweep_hub(
    store: Store,
    embedder: Any,
    *,
    hub_ref_id: int,
    title: str,
    meta: dict[str, Any],
    negate_fn: NegateFn,
    verify_fn: VerifyFn,
    topk: int,
    verify_budget: int,
) -> _SweepResult:
    """Negate → search → rank/floor → verify → file → stamp, for one hub."""
    sentence = title.strip()
    scope = {str(k): str(v) for k, v in (meta.get("scope") or {}).items()}

    try:
        raw = negate_fn(sentence, scope)
    except Exception:
        log.warning(
            "conflict_search: negate hook raised for hub %d", hub_ref_id, exc_info=True
        )
        raw = None
    if raw is None:
        # Transient — clear the lease so the hub is retried promptly,
        # WITHOUT stamping the watermark (module docstring step 1).
        try:
            store.update_ref(
                hub_ref_id, meta_patch={"conflict_search_claimed_at": None}
            )
        except NotFound:
            return _SweepResult(
                swept=False, checked=0, disputes_filed=0, llm_errors=1, vanished=True
            )
        return _SweepResult(swept=False, checked=0, disputes_filed=0, llm_errors=1)

    paraphrases = _parse_paraphrases(raw)
    candidates = _discover(
        store,
        embedder,
        hub_ref_id=hub_ref_id,
        claim_sentence=sentence,
        paraphrases=paraphrases,
        topk=topk,
    )
    candidates = _filter_finding_candidates(store, candidates)
    _attach_read_first(store, candidates)
    selected = _select_for_verify(candidates, verify_budget)

    checked = 0
    disputes_filed = 0
    llm_errors = 0
    for cand in selected:
        checked += 1
        try:
            verdict = verify_fn(
                claim=sentence,
                scope=scope,
                target_cite_key=f"{cand.ref_kind}:{cand.ref_id}",
                target_chunk_ord=cand.chunk_ord,
                target_chunk_text=cand.chunk_text,
                source_kind=cand.ref_kind,
            )
        except Exception:
            log.warning(
                "conflict_search: verify hook raised for hub %d candidate ref %d",
                hub_ref_id,
                cand.ref_id,
                exc_info=True,
            )
            verdict = None
        if verdict is None:
            llm_errors += 1
            continue
        if verdict.get("contradicts") is True:
            handle = handle_registry.try_format(
                cand.ref_kind, cand.chunk_id, chunk=True
            )
            # ``support`` is hardcoded "no", never read off the verdict's
            # own ``supports`` field: the edge exists BECAUSE contradicts
            # is True, while the verify prompt decides ``supports``
            # independently -- a "partial" alongside contradicts=True is
            # reachable and would otherwise produce a self-contradictory
            # edge (hub_refine._attach_disputes's same convention).
            relation = validate_relation("disputes", store=store)
            store.add_link(
                src_ref_id=cand.ref_id,
                dst_ref_id=hub_ref_id,
                relation=relation,
                set_by="system",
                meta={
                    "support": "no",
                    "support_reason": verdict.get("support_reason"),
                    "caveats": verdict.get("caveats") or [],
                    "source_handle": handle,
                    "via": "conflict_search",
                },
            )
            disputes_filed += 1

    try:
        store.update_ref(
            hub_ref_id,
            meta_patch={
                "conflict_search": {
                    "version": CONFLICT_SEARCH_VERSION,
                    "at": datetime.now(UTC).isoformat(),
                    "candidates_checked": checked,
                    "disputes_filed": disputes_filed,
                },
                "conflict_search_claimed_at": None,
            },
        )
    except NotFound:
        return _SweepResult(
            swept=False,
            checked=checked,
            disputes_filed=disputes_filed,
            llm_errors=llm_errors,
            vanished=True,
        )

    return _SweepResult(
        swept=True,
        checked=checked,
        disputes_filed=disputes_filed,
        llm_errors=llm_errors,
    )


# ── the pass ─────────────────────────────────────────────────────────────


def run_conflict_search_pass(
    store: Store,
    *,
    embedder: Any,
    limit: int | None = None,
    negate_fn: NegateFn | None = None,
    verify_fn: VerifyFn | None = None,
) -> dict[str, int]:
    """Run one ``conflict_search`` pass: claim up to ``limit`` due claim
    hubs (default :func:`_hubs_per_pass`) and sweep each for opposition.

    ``negate_fn``/``verify_fn`` are the injectable LLM seams for tests
    (defaults :func:`negate_claim` and ``_chase_llm._verify_support_with_
    caveats``). No embedder wired — the whole cycle no-ops (mirrors
    ``hub_refine``'s embedder-unavailable degrade): claiming hubs that
    can never be searched would just strand them behind the lease TTL
    for nothing.

    Returns ``{hubs_claimed, hubs_swept, candidates_checked,
    disputes_filed, llm_errors, skipped}`` — ``hubs_claimed`` is every hub
    picked up by this pass's claim-and-lease (the ``BatchResult.claimed``
    equivalent, so ``hubs_claimed == hubs_swept + (hubs_claimed -
    hubs_swept)`` keeps the wiring's ``claimed == ok + failed`` invariant,
    mirroring ``hub_tagline``'s ``{claimed, ok, failed}`` shape);
    ``hubs_swept`` is every hub that reached a completed, stamped sweep;
    ``skipped`` counts a hub that vanished (deleted) between claim and
    processing.
    """
    result = {
        "hubs_claimed": 0,
        "hubs_swept": 0,
        "candidates_checked": 0,
        "disputes_filed": 0,
        "llm_errors": 0,
        "skipped": 0,
    }
    if embedder is None:
        log.warning("conflict_search: embedder unavailable -- pass no-ops this cycle")
        return result

    hubs_limit = limit if limit is not None else _hubs_per_pass()
    negate = negate_fn or negate_claim
    verify = verify_fn or _verify_support_with_caveats
    topk = _topk()
    verify_budget = _verify_budget()

    claimed_hubs = _claim_hubs(store, limit=hubs_limit)
    result["hubs_claimed"] = len(claimed_hubs)

    for hub_ref_id, title, meta in claimed_hubs:
        outcome = _sweep_hub(
            store,
            embedder,
            hub_ref_id=hub_ref_id,
            title=title,
            meta=meta,
            negate_fn=negate,
            verify_fn=verify,
            topk=topk,
            verify_budget=verify_budget,
        )
        result["candidates_checked"] += outcome.checked
        result["disputes_filed"] += outcome.disputes_filed
        result["llm_errors"] += outcome.llm_errors
        if outcome.swept:
            result["hubs_swept"] += 1
        elif outcome.vanished:
            result["skipped"] += 1

    return result
