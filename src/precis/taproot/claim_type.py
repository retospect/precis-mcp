"""Claim type — the persisted sort of a claim hub, and what each sort
changes about how the graph treats the hub.

``docs/backlog/taproot-claim-model-v2.md`` (design decided 2026-10-10).
The extractor sorts every claim into one of :data:`CLAIM_TYPES`;
:func:`~precis.taproot.hub.mint_hub` persists it as ``refs.meta.claim_type``
with ``claim_type_by='llm'``. A human may reclassify through the web form
on ``/claim/<head>`` or ``precis taproot classify --hub fiN --set <type>``
(``claim_type_by='human'``); LLM paths never overwrite a human value and
the MCP ``edit(kind='finding')`` door refuses the key — it cannot tell a
human from an agent.

**The policy table is static code** (:data:`POLICIES`). Every consumer
reads the table, never the raw string, and the table is keyed by type —
so the model that assigns a type cannot configure its own gate, which is
the design hazard the backlog item names. Today only ``landscape``
deviates from the default: a background sentence that states the common
case for a whole class of systems (the kind a review is made of, the
parent specific claims hang off via ``refines``). It dedups on sentence
alone (:func:`sentence_id_value`), is never widened (an embedding
attractor — fi449493 collected 14 far-field edges), is never a
``disputes`` counterparty (the common case cannot contradict a specific
result), is verified by consensus rather than per edge, and is not
publishable as a nanopub (a signed consensus sentence attributes
nothing; mesh-internal ``[fi<id>]`` cites are all it needs).

The type is deliberately **not** part of the ``(sentence, scope)``
pub_id key (:func:`precis.identity.make_taproot_hub_paper_id`) — a type
in the key would fork the same sentence into two hubs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from precis.errors import BadInput
from precis.identity import normalize_text_for_hash
from precis.store.types import Tag
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    CLAIM_TYPE_DEFINITIONS,
    CLAIM_TYPES,
    claim_hub_predicate_sql,
    coerce_claim_type,
)
from precis.utils.llm.router import LlmRequest, Tier, route

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Re-exported from :mod:`precis.taproot.canon` (which owns them because
#: the extractor prompt embeds the definitions and this module imports
#: canon): the closed sort set and the shared definitions text.
__all__ = ["CLAIM_TYPES", "CLAIM_TYPE_DEFINITIONS", "coerce_claim_type"]
LANDSCAPE = "landscape"

#: ``refs.meta`` keys. ``claim_type_by`` follows the tagline pattern
#: (``workers/hub_tagline.py``): ``'llm'`` is overwritable by a later
#: pass or a human, ``'human'`` is final until a human changes it.
META_CLAIM_TYPE = "claim_type"
META_CLAIM_TYPE_BY = "claim_type_by"
ClaimTypeBy = Literal["llm", "human"]

#: ``ref_identifiers.id_kind`` of the sentence-only identity a
#: ``dedup_sentence_only`` hub also carries (see :func:`sentence_id_value`).
SENTENCE_ID_KIND = "taproot_sentence"

#: Consensus floor for a ``verifier='consensus'`` hub: independent
#: supporters (author-disjoint papers, ``_finding_evidence.
#: _independent_supporter_counts``) at or above this pass. Advisory —
#: never a gate.
CONSENSUS_MIN_INDEPENDENT = 3


@dataclass(frozen=True)
class ClaimTypePolicy:
    """What one claim type changes. Defaults are today's behaviour for
    every hub; a field is overridden per type in :data:`POLICIES` only."""

    #: Enter the widen arms: ``hub_refine``'s due-set, ``inbound_ground``'s
    #: ANN target set, ``chase_trigger``'s embedding refresh and
    #: ``TAPROOT_DUE`` marking.
    widen: bool = True
    #: Be swept for opposition by ``workers.conflict_search``.
    sweep_conflicts: bool = True
    #: May be the ``src`` of a ``disputes`` edge, or a finding counterparty
    #: the conflict sweep offers against another hub.
    disputes_counterparty: bool = True
    #: Also carry the sentence-only identifier, so any later mint of the
    #: same sentence under a different scope converges onto this hub.
    dedup_sentence_only: bool = False
    #: ``per-edge``: every evidence edge needs its own support verdict.
    #: ``consensus``: hub-level — enough independent sources state it.
    verifier: Literal["per-edge", "consensus"] = "per-edge"
    #: ``nanopub.mint.approve`` accepts the hub.
    publishable: bool = True


_DEFAULT_POLICY = ClaimTypePolicy()

#: The static per-type table. Absent / unknown type → the default policy.
POLICIES: Mapping[str, ClaimTypePolicy] = {
    "measurement": _DEFAULT_POLICY,
    "definition": _DEFAULT_POLICY,
    "capability": _DEFAULT_POLICY,
    "mechanism": _DEFAULT_POLICY,
    LANDSCAPE: ClaimTypePolicy(
        widen=False,
        sweep_conflicts=False,
        disputes_counterparty=False,
        dedup_sentence_only=True,
        verifier="consensus",
        publishable=False,
    ),
}
assert set(POLICIES) == set(CLAIM_TYPES)


def claim_type_of(meta: Mapping[str, Any] | None) -> str | None:
    """The hub's persisted type, read off ``refs.meta``."""
    return coerce_claim_type((meta or {}).get(META_CLAIM_TYPE))


def policy_for(claim_type: str | None) -> ClaimTypePolicy:
    """The policy row for a type (or for "no type": the default)."""
    if claim_type is None:
        return _DEFAULT_POLICY
    return POLICIES.get(claim_type, _DEFAULT_POLICY)


# ── SQL predicates ────────────────────────────────────────────────────────


def _excluding_types_sql(types: tuple[str, ...], *, ref_alias: str) -> str:
    """``TRUE`` when the hub's type is not one of ``types`` (absent type
    passes). Literals, not binds: the values come from the static table
    and are validated members of :data:`CLAIM_TYPES`, so callers can AND
    the clause onto a query without threading a new parameter."""
    if not types:
        return "TRUE"
    assert all(t in POLICIES and t.isalpha() for t in types)
    quoted = ", ".join(f"'{t}'" for t in types)
    return f"(COALESCE({ref_alias}.meta->>'{META_CLAIM_TYPE}', '') NOT IN ({quoted}))"


def _types_where(pred: Callable[[ClaimTypePolicy], bool]) -> tuple[str, ...]:
    return tuple(t for t in CLAIM_TYPES if pred(POLICIES[t]))


NO_WIDEN_TYPES: tuple[str, ...] = _types_where(lambda p: not p.widen)
NO_SWEEP_TYPES: tuple[str, ...] = _types_where(lambda p: not p.sweep_conflicts)
NO_DISPUTES_TYPES: tuple[str, ...] = _types_where(lambda p: not p.disputes_counterparty)


def widenable_predicate_sql(*, ref_alias: str = "r") -> str:
    """AND this onto :func:`~precis.taproot.canon.claim_hub_predicate_sql`
    in every pass that goes looking for more evidence for a hub
    (``hub_refine``, ``inbound_ground``, ``chase_trigger``) — the sibling
    of :func:`~precis.taproot.canon.not_hypothesis_predicate_sql` for
    types whose policy has ``widen=False``."""
    return _excluding_types_sql(NO_WIDEN_TYPES, ref_alias=ref_alias)


def sweepable_predicate_sql(*, ref_alias: str = "r") -> str:
    """Hubs ``workers.conflict_search`` may sweep for opposition."""
    return _excluding_types_sql(NO_SWEEP_TYPES, ref_alias=ref_alias)


def disputes_counterparty_predicate_sql(*, ref_alias: str = "r") -> str:
    """Hubs that may stand as the ``src`` of a ``disputes`` edge."""
    return _excluding_types_sql(NO_DISPUTES_TYPES, ref_alias=ref_alias)


# ── sentence-only identity ────────────────────────────────────────────────


def sentence_id_value(sentence: str) -> str:
    """``ref_identifiers.id_value`` for a sentence-only hub identity:
    SHA-256 of :func:`~precis.identity.normalize_text_for_hash` on the
    sentence, scope-free. Same normalization as the pub_id key minus the
    scope, so the two identities agree on what "the same sentence" is."""
    digest = hashlib.sha256(
        normalize_text_for_hash(sentence).encode("utf-8")
    ).hexdigest()
    return f"taproot-sentence:{digest}"


def find_hub_by_sentence(conn: Any, sentence: str) -> int | None:
    """The live hub registered as holding this exact sentence (a
    ``dedup_sentence_only`` hub), or ``None``."""
    row = conn.execute(
        """
        SELECT ri.ref_id
          FROM ref_identifiers ri
          JOIN refs r ON r.ref_id = ri.ref_id
         WHERE ri.id_kind = %s AND ri.id_value = %s
           AND r.retired_at IS NULL
        """,
        (SENTENCE_ID_KIND, sentence_id_value(sentence)),
    ).fetchone()
    return int(row[0]) if row is not None else None


def register_sentence_id(conn: Any, hub_ref_id: int, sentence: str) -> int | None:
    """Register ``hub_ref_id`` as the holder of ``sentence``. Returns
    ``None`` when this hub now holds (or already held) the identifier, or
    the ref_id of a **different** live hub that already holds it — a
    duplicate-pair signal the caller reports as a merge candidate rather
    than fails on (the identifier is a dedup aid, not a constraint on
    classification)."""
    holder = find_hub_by_sentence(conn, sentence)
    if holder is not None:
        return None if holder == hub_ref_id else holder
    conn.execute(
        "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (SENTENCE_ID_KIND, sentence_id_value(sentence), hub_ref_id, "taproot"),
    )
    return None


# ── the write door ────────────────────────────────────────────────────────

#: ``TAPROOT_DUE`` — the review-queue marker ``chase_trigger`` sets and
#: ``hub_refine`` pops. Mirrored here (not imported) because both workers
#: import taproot, not the other way round.
_DUE_NS = "TAPROOT_DUE"
_DUE_VALUE = "1"


def set_claim_type(
    store: Store,
    hub_ref_id: int,
    claim_type: str | None,
    *,
    by: ClaimTypeBy,
    conn: Any = None,
) -> dict[str, Any]:
    """The one write door for ``refs.meta.claim_type`` on an existing hub.

    * ``by='human'`` always writes; ``claim_type=None`` clears both keys
      (handing the hub back to the classify pass).
    * ``by='llm'`` never overwrites a human classification — returns
      ``{"skipped": "human"}`` — and never clears.
    * A type whose policy is ``dedup_sentence_only`` also registers the
      sentence identifier (:func:`register_sentence_id`); another live hub
      already holding the sentence is reported as ``duplicate_of``.
    * A type whose policy is ``widen=False`` pops a pending ``TAPROOT_DUE``
      — the per-edge review that marker queues does not apply.

    Raises :class:`BadInput` on an unknown type or a non-hub ref.
    """
    if claim_type is not None and coerce_claim_type(claim_type) is None:
        raise BadInput(
            f"unknown claim_type {claim_type!r}",
            next=f"one of {list(CLAIM_TYPES)}",
        )
    claim_type = coerce_claim_type(claim_type)
    if by == "llm" and claim_type is None:
        raise BadInput("the LLM path cannot clear a claim_type", next="pass by='human'")

    def _do(c: Any) -> dict[str, Any]:
        row = c.execute(
            f"""
            SELECT r.title, r.meta FROM refs r
             WHERE r.ref_id = %(rid)s AND r.kind = 'finding'
               AND r.retired_at IS NULL AND {claim_hub_predicate_sql()}
            """,
            {"rid": hub_ref_id, **CLAIM_HUB_PREDICATE_PARAMS},
        ).fetchone()
        if row is None:
            raise BadInput(
                f"ref {hub_ref_id} is not a live claim hub",
                next="claim_type applies to TAPROOT:claim hubs only",
            )
        title, meta = str(row[0]), dict(row[1] or {})
        previous = claim_type_of(meta)
        previous_by = meta.get(META_CLAIM_TYPE_BY)
        if by == "llm" and previous_by == "human":
            return {
                "hub_ref_id": hub_ref_id,
                "skipped": "human",
                "claim_type": previous,
            }
        patch: dict[str, Any] = {
            META_CLAIM_TYPE: claim_type,
            META_CLAIM_TYPE_BY: by if claim_type is not None else None,
        }
        store.update_ref(hub_ref_id, meta_patch=patch, conn=c)
        out: dict[str, Any] = {
            "hub_ref_id": hub_ref_id,
            "claim_type": claim_type,
            "previous": previous,
            "by": by,
        }
        policy = policy_for(claim_type)
        if policy.dedup_sentence_only:
            other = register_sentence_id(c, hub_ref_id, title)
            if other is not None:
                out["duplicate_of"] = other
                log.warning(
                    "claim_type: hub %d classified %s but hub %d already holds "
                    "the same sentence — merge candidate",
                    hub_ref_id,
                    claim_type,
                    other,
                )
        if not policy.widen:
            popped = store.remove_tag(
                hub_ref_id, Tag.closed(_DUE_NS, _DUE_VALUE), conn=c
            )
            out["popped_due"] = bool(popped)
        return out

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


# ── consensus (the landscape verifier) ────────────────────────────────────


def consensus_line(claim_type: str | None, independent: int) -> str | None:
    """The hub-level verdict line for a ``verifier='consensus'`` hub, or
    ``None`` for every other type (which keep per-edge verdicts).
    ``independent`` is the author-disjoint supporter count."""
    if policy_for(claim_type).verifier != "consensus":
        return None
    verdict = "pass" if independent >= CONSENSUS_MIN_INDEPENDENT else "fail"
    return (
        f"consensus ({claim_type}): {independent} independent sources, "
        f"{verdict} (floor {CONSENSUS_MIN_INDEPENDENT}; verified at hub level, "
        "not per edge)"
    )


# ── the classifier (backfill) ─────────────────────────────────────────────

_PROMPT_CLASSIFY = (
    """\
Sort this scientific claim into exactly one type.

CLAIM: {sentence}
SCOPE: {scope_json}

"""
    + CLAIM_TYPE_DEFINITIONS
    + """

Respond with EXACTLY ONE JSON object, nothing else:
{{"type": "<measurement|definition|capability|mechanism|landscape>"}}
"""
)

ClassifyFn = Callable[[str, dict[str, Any]], str | None]


def classify_sentence(sentence: str, scope: dict[str, Any]) -> str | None:
    """One MEDIUM-tier sort of ``sentence``. ``None`` on dispatch failure
    or an out-of-set answer — never a verdict from a model that didn't
    run."""
    prompt = _PROMPT_CLASSIFY.format(
        sentence=sentence, scope_json=json.dumps(scope, sort_keys=True)
    )
    res = route(LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="taproot:classify"))
    if res.error:
        log.warning("claim_type: classify failed: %s", res.error)
        return None
    data = res.data if isinstance(res.data, dict) else {}
    return coerce_claim_type(data.get("type"))


_UNCLASSIFIED_SQL = f"""\
    SELECT r.ref_id, r.title, r.meta
      FROM refs r
     WHERE r.kind = 'finding'
       AND r.retired_at IS NULL
       AND {claim_hub_predicate_sql()}
       AND (r.meta->>'artifact_type') IS DISTINCT FROM 'hypothesis'
       AND r.meta->>'{META_CLAIM_TYPE}' IS NULL
       AND (%(hub)s::bigint IS NULL OR r.ref_id = %(hub)s)
     ORDER BY r.ref_id
     LIMIT %(limit)s
"""


def run_classify_pass(
    store: Store,
    *,
    limit: int,
    apply: bool,
    classify_fn: ClassifyFn = classify_sentence,
    hub_ref_id: int | None = None,
    workers: int = 1,
    batch_size: int | None = None,
    on_batch: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Classify up to ``limit`` hubs that have no ``claim_type`` yet (one
    paid call each) and, under ``apply``, persist with ``by='llm'``.
    Idempotent: a hub with a type is never re-asked, so a re-run after a
    partial pass continues where it stopped. Returns one row per hub:
    ``{hub_ref_id, title, claim_type, applied, ...}``; a ``None`` type is
    a counted skip, never a write.

    ``workers`` > 1 issues the paid calls from a thread pool (a MEDIUM
    call is ~15 s of latency, so 3.9k hubs sequentially is a working day);
    the writes stay sequential in the calling thread, in ``ref_id`` order.

    Work proceeds in batches of ``batch_size`` (default ``4 * workers``):
    each batch is classified, then written, then handed to ``on_batch``
    before the next batch's calls go out -- so a run killed mid-way keeps
    every finished batch (and its ``--out`` rows), not nothing."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            _UNCLASSIFIED_SQL,
            {"hub": hub_ref_id, "limit": limit, **CLAIM_HUB_PREDICATE_PARAMS},
        ).fetchall()

    def _classify(ref_id: int, title: str, meta: Any) -> str | None:
        scope = {str(k): str(v) for k, v in ((meta or {}).get("scope") or {}).items()}
        try:
            return classify_fn(str(title), scope)
        except Exception:
            log.warning("claim_type: classify raised for hub %d", ref_id, exc_info=True)
            return None

    step = max(1, batch_size if batch_size is not None else 4 * max(1, workers))
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    out: list[dict[str, Any]] = []
    try:
        for start in range(0, len(rows), step):
            batch = rows[start : start + step]
            if pool is not None and len(batch) > 1:
                types = list(
                    pool.map(lambda r: _classify(int(r[0]), r[1], r[2]), batch)
                )
            else:
                types = [_classify(int(r[0]), r[1], r[2]) for r in batch]
            done: list[dict[str, Any]] = []
            for (ref_id, title, _meta), claim_type in zip(batch, types, strict=True):
                row: dict[str, Any] = {
                    "hub_ref_id": int(ref_id),
                    "title": str(title),
                    "claim_type": claim_type,
                    "applied": False,
                }
                if claim_type is not None and apply:
                    result = set_claim_type(store, int(ref_id), claim_type, by="llm")
                    row["applied"] = "skipped" not in result
                    if "duplicate_of" in result:
                        row["duplicate_of"] = result["duplicate_of"]
                done.append(row)
            if on_batch is not None:
                on_batch(done)
            out.extend(done)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    return out
