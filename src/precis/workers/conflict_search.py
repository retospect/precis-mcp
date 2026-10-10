"""``conflict_search`` — every claim hub hunts its own opposition.

Slice 1 of ``docs/backlog/claim-conflict-search.md`` (items 1-3: search,
budgeted verify, coverage ledger; items 4-5, the approve-time advisory
panel and the counter-claim mint, are later slices). A standing,
watermarked ref-pass — mirrors ``hub_tagline``'s claim-and-lease shape,
not ``hub_refine``'s single-transaction discover→verify→write spine
(this pass is simpler: no composite-hub handling, no rejection memo, no
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
   candidates_checked, disputes_filed, covered}`` written unconditionally
   on a completed sweep (even a hub with zero candidates: "no known
   conflict as of <date>" is a checkable statement, not silence), and the
   claim lease cleared. Coverage (swept/total at the live version) is
   then one query (:func:`coverage_counts`). ``covered`` is the per-
   passage ledger — one ``{ref_id, kind, handle, verdict}`` row per
   verified candidate (``verdict`` is ``"disputes"`` or
   ``"no-conflict"``; a verify dispatch failure is not a verdict and is
   never recorded as covered). A re-sweep at the *same* version skips
   every chunk already in ``covered`` before spending budget and merges
   the new rows in, so the ledger is what makes "never repeat work" true
   for the approve-time refresh below; a version bump discards it (the
   method changed, so the old verdicts are not comparable).

**Three doors onto the same sweep.** The standing pass
(:func:`run_conflict_search_pass`) is the retro backfill: it walks the
watermark cohort a few hubs per tick. :func:`sweep_one_hub` is the same
negate→search→verify→file→stamp for ONE named hub, with the same lease
and the same watermark rule unless ``refresh=True`` — it is what the
``conflict_sweep`` job type (``workers/job_types/conflict_sweep.py``)
runs. :func:`enqueue_conflict_sweep` mints that job: ``taproot.hub.
mint_hub`` calls it inside the mint savepoint so a freshly minted claim
is swept promptly rather than waiting for the backfill walk to reach it,
and the nanopub approve surface (``nanopub/mint.py::approve``,
``precis_web/nanopub_render.py``) calls it with ``refresh=True`` when
:func:`coverage_status` says the ledger is missing, from an older method
version, or older than :data:`CONFLICT_SEARCH_FRESH_DAYS`. Every door is
gated on the same ``service_config`` row (``conflict_search`` prio > 0 on
any host) — a dark service mints no jobs, so enabling the pass is the
one switch for all three populations. The enqueue is idempotent on
``meta.idem_key`` (``conflict_sweep:<hub>:v<version>:<reason>``), so a
mint and a same-day approve-page refresh never queue two sweeps.

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
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from precis.errors import NotFound
from precis.store import Store
from precis.store.types import Tag
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    claim_hub_predicate_sql,
    not_hypothesis_predicate_sql,
)
from precis.taproot.claim_type import (
    disputes_counterparty_predicate_sql,
    sweepable_predicate_sql,
)
from precis.taproot.hub import HUB_ROLES
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.llm.router import LlmRequest, Tier, route
from precis.utils.relations import validate_relation
from precis.workers._chase_llm import _verify_support_with_caveats

log = logging.getLogger(__name__)

__all__ = [
    "CONFLICT_SEARCH_FRESH_DAYS",
    "CONFLICT_SEARCH_VERSION",
    "CONFLICT_SWEEP_JOB_TYPE",
    "ConflictCoverage",
    "NegateFn",
    "SweepOutcome",
    "VerifyFn",
    "conflict_search_enabled",
    "coverage_counts",
    "coverage_status",
    "enqueue_conflict_sweep",
    "negate_claim",
    "run_conflict_search_pass",
    "sweep_one_hub",
]

#: Bump to re-sweep every hub (the watermark rule, module docstring).
CONFLICT_SEARCH_VERSION = 1

#: A ledger whose ``at`` is older than this is ``stale-age`` at approve
#: time (:func:`coverage_status`) — the corpus has grown since, so the
#: "no known conflict" statement is re-checked before the claim freezes.
#: Starting point, not tuned: the approve refresh only verifies chunks
#: NOT already in ``covered``, so a shorter window costs little.
CONFLICT_SEARCH_FRESH_DAYS = 90

#: The job type :func:`enqueue_conflict_sweep` mints — one named hub's
#: sweep on the ``claude_inproc`` lane (``workers/job_types/conflict_sweep.py``).
CONFLICT_SWEEP_JOB_TYPE = "conflict_sweep"

#: The ``service_config`` service every door of this module is gated on.
_SERVICE_NAME = "conflict_search"

#: Background priority for a minted sweep job — same tier the other
#: system-minted maintenance jobs use (``draft_refresh_scan``,
#: ``diagnose_scan``: ``_MINT_PRIO = 8``).
_SWEEP_JOB_PRIO = 8

#: Upper bound on ``meta.conflict_search.covered`` rows kept per hub —
#: oldest rows fall off first (and so become re-verifiable). Six verify
#: slots per sweep means this is years of daily refreshes for one hub.
_COVERED_CAP = 200

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

#: A live claim hub, not a hypothesis, whose claim-type policy allows a
#: conflict sweep (not ``landscape``) — the population every door sweeps.
_LIVE_HUB_SQL = f"""\
    r.kind = 'finding'
       AND r.retired_at IS NULL
       AND {claim_hub_predicate_sql()}
       AND {not_hypothesis_predicate_sql()}
       AND {sweepable_predicate_sql()}
"""

#: The watermark rule: coverage missing, or from an older method version.
_DUE_SQL = """\
    (
             r.meta->'conflict_search'->>'version' IS NULL
             OR (r.meta->'conflict_search'->>'version')::int < %(version)s
    )
"""

#: Not currently leased by another in-flight sweep (TTL-expired counts as free).
_LEASE_FREE_SQL = """\
    (r.meta->>'conflict_search_claimed_at' IS NULL
            OR (r.meta->>'conflict_search_claimed_at')::timestamptz
                 < now() - make_interval(mins => %(ttl_min)s))
"""

#: Live claim hubs, not a hypothesis, whose conflict-search coverage is
#: missing or stale, not currently leased by another node's in-flight
#: sweep. Mirrors ``hub_tagline.py``'s ``_COHORT_SQL`` shape.
_COHORT_SQL = f"""\
    SELECT r.ref_id, r.title, r.meta
      FROM refs r
     WHERE {_LIVE_HUB_SQL}
       AND {_DUE_SQL}
       AND {_LEASE_FREE_SQL}
     ORDER BY r.ref_id
     LIMIT %(limit)s
       FOR UPDATE OF r SKIP LOCKED
"""


def _claim_hubs(store: Store, *, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    """Atomically claim up to ``limit`` due hubs: ``(ref_id, title, meta)``.

    Same ``UPDATE ... FROM MATERIALIZED-CTE (SELECT ... FOR UPDATE SKIP
    LOCKED) ... RETURNING`` idiom as ``hub_tagline._claim_candidates``
    (see there for why the cohort must be a ``MATERIALIZED`` CTE, never an
    inline subquery — a planner rescan over-claims past ``limit``) — stamps
    ``meta.conflict_search_claimed_at`` atomically so two racing nodes
    never both pay for the same hub's sweep within the lease TTL.
    """
    if limit <= 0:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            WITH c AS MATERIALIZED ({_COHORT_SQL})
            UPDATE refs r
               SET meta = r.meta || jsonb_build_object(
                             'conflict_search_claimed_at', now()::text)
              FROM c
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


def _claim_one_hub(
    store: Store, hub_ref_id: int, *, refresh: bool
) -> tuple[str, dict[str, Any]] | None:
    """Atomically claim ONE named hub for a sweep: ``(title, meta)``, or
    ``None`` when it is not claimable — not a live claim hub, leased by
    an in-flight sweep, or (unless ``refresh``) already at the current
    version. Same lease stamp as :func:`_claim_hubs`, so a job and the
    standing pass never both pay for the same hub inside the TTL."""
    due = "TRUE" if refresh else _DUE_SQL
    with store.pool.connection() as conn:
        row = conn.execute(
            f"""
            UPDATE refs r
               SET meta = r.meta || jsonb_build_object(
                             'conflict_search_claimed_at', now()::text)
             WHERE r.ref_id = %(hub)s
               AND {_LIVE_HUB_SQL}
               AND {due}
               AND {_LEASE_FREE_SQL}
             RETURNING r.title, r.meta
            """,
            {
                **CLAIM_HUB_PREDICATE_PARAMS,
                **NOT_HYPOTHESIS_PREDICATE_PARAMS,
                "hub": hub_ref_id,
                "version": CONFLICT_SEARCH_VERSION,
                "ttl_min": _CLAIM_TTL_MIN,
            },
        ).fetchone()
        conn.commit()
    if row is None:
        return None
    return str(row[0] or ""), dict(row[1] or {})


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
#: findings as hubs"). Also drops hubs whose claim-type policy forbids
#: being a disputes counterparty (``landscape``).
_FINDING_CANDIDATE_SQL = f"""\
    SELECT r.ref_id
      FROM refs r
     WHERE r.ref_id = ANY(%(ref_ids)s)
       AND r.kind = 'finding'
       AND r.retired_at IS NULL
       AND {claim_hub_predicate_sql()}
       AND {not_hypothesis_predicate_sql()}
       AND {disputes_counterparty_predicate_sql()}
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
class SweepOutcome:
    """One hub's sweep result. ``swept`` is a completed, stamped sweep;
    ``vanished`` means the hub was deleted between claim and stamp.
    ``checked`` counts verify calls spent this sweep (dispatch failures
    included — they are in ``llm_errors`` too); ``skipped_covered`` is
    how many discovered chunks the ledger let this sweep skip."""

    swept: bool
    checked: int
    disputes_filed: int
    llm_errors: int
    vanished: bool = False
    skipped_covered: int = 0


#: Backwards-compatible alias for the pre-slice-2 private name.
_SweepResult = SweepOutcome


def _prior_ledger(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The hub's existing ``meta.conflict_search`` iff it is at the
    current version — an older version's verdicts came from a different
    method and are not carried forward."""
    prior = meta.get("conflict_search")
    if not isinstance(prior, dict):
        return None
    try:
        if int(prior.get("version") or 0) != CONFLICT_SEARCH_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    return prior


def _covered_rows(ledger: dict[str, Any] | None) -> list[dict[str, Any]]:
    if ledger is None:
        return []
    raw = ledger.get("covered")
    return (
        [dict(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    )


def _covered_chunk_ids(rows: list[dict[str, Any]]) -> set[int]:
    """Chunk ids already verified. A row without a usable ``chunk_id`` is
    simply not skipped — re-verifying is the safe failure."""
    out: set[int] = set()
    for r in rows:
        try:
            out.add(int(r["chunk_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
    # The ledger: chunks verified by an earlier sweep at this version
    # never take a budget slot again (module docstring step 6).
    prior = _prior_ledger(meta)
    covered = _covered_rows(prior)
    already = _covered_chunk_ids(covered)
    fresh_candidates = [c for c in candidates if c.chunk_id not in already]
    skipped_covered = len(candidates) - len(fresh_candidates)
    _attach_read_first(store, fresh_candidates)
    selected = _select_for_verify(fresh_candidates, verify_budget)

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
        handle = handle_registry.try_format(cand.ref_kind, cand.chunk_id, chunk=True)
        contradicts = verdict.get("contradicts") is True
        covered.append(
            {
                "ref_id": cand.ref_id,
                "kind": cand.ref_kind,
                "chunk_id": cand.chunk_id,
                "handle": handle,
                "verdict": "disputes" if contradicts else "no-conflict",
            }
        )
        if contradicts:
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

    # Cumulative at this version: the ledger merges, never resets, until
    # the method version bumps (``_prior_ledger`` drops an older one).
    covered = covered[-_COVERED_CAP:]
    prior_filed = int((prior or {}).get("disputes_filed") or 0)
    try:
        store.update_ref(
            hub_ref_id,
            meta_patch={
                "conflict_search": {
                    "version": CONFLICT_SEARCH_VERSION,
                    "at": datetime.now(UTC).isoformat(),
                    "candidates_checked": len(covered),
                    "disputes_filed": prior_filed + disputes_filed,
                    "covered": covered,
                },
                "conflict_search_claimed_at": None,
            },
        )
    except NotFound:
        return SweepOutcome(
            swept=False,
            checked=checked,
            disputes_filed=disputes_filed,
            llm_errors=llm_errors,
            vanished=True,
            skipped_covered=skipped_covered,
        )

    return SweepOutcome(
        swept=True,
        checked=checked,
        disputes_filed=disputes_filed,
        llm_errors=llm_errors,
        skipped_covered=skipped_covered,
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


# ── one named hub (the job type's door) ──────────────────────────────────


def sweep_one_hub(
    store: Store,
    *,
    embedder: Any,
    hub_ref_id: int,
    refresh: bool = False,
    negate_fn: NegateFn | None = None,
    verify_fn: VerifyFn | None = None,
) -> SweepOutcome | None:
    """Sweep ONE named claim hub — the same negate → search → rank/floor →
    verify → file → stamp as the standing pass, for the ``conflict_sweep``
    job type. Claims the hub through the same lease as the pass
    (:func:`_claim_one_hub`); honours the watermark unless ``refresh`` —
    the approve-time freshness re-sweep sets it, and the ledger (module
    docstring step 6) keeps a refresh from re-verifying anything already
    covered.

    Returns ``None`` when the hub is not claimable (not a live claim hub,
    already at the current version without ``refresh``, or leased by an
    in-flight sweep) — a no-op, not a failure. ``embedder is None`` is
    also ``None``, mirroring the pass's degrade.
    """
    if embedder is None:
        log.warning("conflict_search: embedder unavailable -- sweep_one_hub no-ops")
        return None
    claimed = _claim_one_hub(store, hub_ref_id, refresh=refresh)
    if claimed is None:
        return None
    title, meta = claimed
    return _sweep_hub(
        store,
        embedder,
        hub_ref_id=hub_ref_id,
        title=title,
        meta=meta,
        negate_fn=negate_fn or negate_claim,
        verify_fn=verify_fn or _verify_support_with_caveats,
        topk=_topk(),
        verify_budget=_verify_budget(),
    )


# ── enqueue (mint trigger + approve-time refresh) ────────────────────────


def conflict_search_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row enables ``conflict_search`` (prio
    > 0) on ANY host. The mint trigger and the approve refresh run in the
    MCP/web process, which is never the host that runs the pass, so the
    per-host resolver (``ServiceConfigResolver``) is the wrong question
    here — "is the service lit anywhere" is. Never raises: a missing
    table or a connection blip reads as dark (no job minted), the same
    fail-closed default the resolver has."""

    def _q(c: Any) -> bool:
        row = c.execute(
            "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
            (_SERVICE_NAME,),
        ).fetchone()
        return row is not None

    try:
        if conn is not None:
            return _q(conn)
        with store.pool.connection() as c:
            return _q(c)
    except Exception:
        log.warning("conflict_search: service_config lookup failed", exc_info=True)
        return False


def _sweep_idem_key(hub_ref_id: int, reason: str) -> str:
    return f"{CONFLICT_SWEEP_JOB_TYPE}:{hub_ref_id}:v{CONFLICT_SEARCH_VERSION}:{reason}"


def enqueue_conflict_sweep(
    store: Store,
    hub_ref_id: int,
    *,
    reason: str,
    refresh: bool = False,
    conn: Any = None,
) -> int | None:
    """Mint ONE ``conflict_sweep`` job for ``hub_ref_id``, or return the
    live job already holding its idem key. ``None`` when the service is
    dark (:func:`conflict_search_enabled`) — a dark service mints no
    jobs, so the ``service_config`` row is the one switch for the pass,
    the mint trigger and the approve refresh alike.

    ``reason`` scopes the idem key (``conflict_sweep:<hub>:v<version>:
    <reason>``): ``"mint"`` from :func:`precis.taproot.hub.mint_hub`,
    ``"refresh:<UTC date>"`` from the approve surface (at most one
    refresh per hub per day). Same direct-``insert_ref`` + idem-guarded
    shape as ``draft_refresh_scan._mint`` / ``diagnose_scan._mint``:
    parentless, ``STATUS:queued``, ``claude_inproc``. ``conn=`` joins the
    caller's transaction (the mint savepoint) so a rolled-back mint
    leaves no orphan job.
    """

    def _do(c: Any) -> int | None:
        if not conflict_search_enabled(store, conn=c):
            return None
        idem_key = _sweep_idem_key(hub_ref_id, reason)
        existing = c.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'idem_key' = %s LIMIT 1",
            (idem_key,),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=f"{CONFLICT_SWEEP_JOB_TYPE} (fi{hub_ref_id}: {reason})",
            meta={
                "job_type": CONFLICT_SWEEP_JOB_TYPE,
                "executor": "claude_inproc",
                "params": {"hub_id": hub_ref_id, "refresh": bool(refresh)},
                "idem_key": idem_key,
            },
            prio=_SWEEP_JOB_PRIO,
            conn=c,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=c,
        )
        log.info(
            "conflict_search: minted %s job id=%d for fi%d (%s)",
            CONFLICT_SWEEP_JOB_TYPE,
            ref.id,
            hub_ref_id,
            reason,
        )
        return int(ref.id)

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


# ── coverage reads (the ledger as a statement) ───────────────────────────

CoverageStatus = Literal["fresh", "stale-age", "stale-version", "missing"]


@dataclass(frozen=True)
class ConflictCoverage:
    """One hub's ``meta.conflict_search`` read as a checkable statement.

    ``status``: ``fresh`` (swept by the current method within
    :data:`CONFLICT_SEARCH_FRESH_DAYS`), ``stale-age`` (current method,
    older sweep), ``stale-version`` (an earlier method), ``missing``
    (never swept — says nothing about opposition, in either direction).
    """

    status: CoverageStatus
    version: int | None
    at: datetime | None
    candidates_checked: int
    disputes_filed: int
    covered: list[dict[str, Any]]

    @property
    def fresh(self) -> bool:
        return self.status == "fresh"


def coverage_status(
    meta: dict[str, Any] | None, *, now: datetime | None = None
) -> ConflictCoverage:
    """Pure read of a hub's ledger — no DB. Malformed fields degrade to
    ``missing`` rather than raise: the approve page must render whatever
    an older sweep wrote."""
    ledger = (meta or {}).get("conflict_search")
    if not isinstance(ledger, dict):
        return ConflictCoverage("missing", None, None, 0, 0, [])
    try:
        raw_version = ledger.get("version")
        if not isinstance(raw_version, (int, str)):
            raise TypeError("version missing")
        version = int(raw_version)
    except (TypeError, ValueError):
        return ConflictCoverage("missing", None, None, 0, 0, [])
    at: datetime | None
    try:
        at = datetime.fromisoformat(str(ledger.get("at")))
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        at = None

    def _int(key: str) -> int:
        try:
            return int(ledger.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    covered = _covered_rows(ledger)
    status: CoverageStatus
    if version < CONFLICT_SEARCH_VERSION:
        status = "stale-version"
    elif at is None or (now or datetime.now(UTC)) - at > timedelta(
        days=CONFLICT_SEARCH_FRESH_DAYS
    ):
        status = "stale-age"
    else:
        status = "fresh"
    return ConflictCoverage(
        status, version, at, _int("candidates_checked"), _int("disputes_filed"), covered
    )


def coverage_counts(store: Store) -> dict[str, int]:
    """``{swept, total, version}`` — how many live claim hubs carry a
    ledger at the current method version, out of all live claim hubs. The
    "coverage is one query" acceptance criterion; bumping the version
    drops ``swept`` to zero until the backfill walks everyone again."""
    with store.pool.connection() as conn:
        row = conn.execute(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (
                     WHERE (r.meta->'conflict_search'->>'version')::int
                           >= %(version)s
                   ) AS swept
              FROM refs r
             WHERE {_LIVE_HUB_SQL}
            """,
            {
                **CLAIM_HUB_PREDICATE_PARAMS,
                **NOT_HYPOTHESIS_PREDICATE_PARAMS,
                "version": CONFLICT_SEARCH_VERSION,
            },
        ).fetchone()
    total, swept = (int(row[0]), int(row[1])) if row is not None else (0, 0)
    return {"swept": swept, "total": total, "version": CONFLICT_SEARCH_VERSION}
