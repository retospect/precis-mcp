"""Certify withheld/unverified evidence edges for the publish preflight.

``nanopub/preflight.py::withheld_edges`` blocks publication on any inbound
``establishes``/``corroborates`` edge carrying neither ``links.meta.support``
nor a human ``publish_signoff``. This sweep (``precis taproot verify-edges``,
dry-run by default) is the verifier that debt has been waiting on: it re-reads
each edge's pinned passage against the hub's claim via the minter's own
verifier (:func:`precis.workers._chase_llm._verify_support_with_caveats`,
MEDIUM) and writes the ``meta.support`` the preflight reads. The 2026-08-27
audit found 264 withheld edges across 248 hubs — mostly campaign/reground-era
attaches — plus the born-released cohort below.

Distinct from reground's strict judge, which decides whether an edge should
EXIST (KEEP/PRUNE/CONTRADICTS, memoed in ``meta.reground_seen``): this pass
only certifies edges for the publish gate and never prunes. A
non-corroborating verdict (the attach gate is
:func:`~precis.workers._chase_llm.is_corroborating`, shared with
``hub_refine``'s write door) is **never stamped** — pruning stays
reground's door.

Two cohorts, same verify, different write:

* **withheld** (default, :func:`select_withheld_edges`): ``support`` absent,
  no ``publish_signoff``, a pinned passage (``src_chunk_id``). Passage-less
  rows are ``repair-evidence`` territory — counted
  (:func:`count_passageless_edges`), never verified here. A non-corroborating
  verdict is reported for reground/human follow-up; nothing is written.
* **unverified-stamped** (``--unverified-stamped``,
  :func:`select_unverified_stamped_edges`): ``support`` present but no
  ``verified_by`` — the born-released cohort: three attach paths wrote a
  default ``support: "yes"`` at mint time that nothing ever read (1252 of
  1461 stamped edges, measured 2026-08-21), and a 2026-08-27 external review
  requires re-verifying them. A corroborating verdict OVERWRITES the stamp
  with the real one; a non-corroborating verdict STRIPS the ``support`` key,
  returning the edge to withheld behind the publish gate.

The stamp (:func:`verify_edge` with ``apply=True``, corroborating only)
jsonb-merges ``support`` / ``support_reason`` / ``caveats`` /
``verified_by: 'verify-edges'`` / ``verified_at`` / ``verified_claim_sha``
into the edge's meta, preserving unrelated keys. ``verified_claim_sha``
(:func:`precis.taproot.canon.claim_sha` of the hub sentence at verify time)
is claim-edit invalidation: ``nanopub/preflight.py::withheld_edges`` treats
an edge whose sha no longer matches the live title as stale-verified, so an
edited claim re-enters this sweep instead of keeping a stale verdict. Each
edge's write runs in its own short transaction, so a crash mid-sweep loses
nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from precis.taproot.canon import CLAIM_HUB_PREDICATE_PARAMS, claim_sha
from precis.workers._chase_llm import _verify_support_with_caveats, is_corroborating

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "VERIFIED_BY",
    "CandidateEdge",
    "EdgeVerifyResult",
    "EdgeVerifyStatus",
    "count_passageless_edges",
    "select_unverified_stamped_edges",
    "select_withheld_edges",
    "verify_edge",
]

#: The exact ``meta.verified_by`` value this sweep writes — the fingerprint
#: that distinguishes its verdicts from refine's and from mint-time defaults.
VERIFIED_BY = "verify-edges"

EdgeVerifyStatus = Literal[
    "verified",
    "not-corroborated",
    "stripped",
    "llm-failed",
    "chunk-missing",
]

#: Corroborating verdict — the only status that stamps (under ``apply``).
STATUS_VERIFIED: EdgeVerifyStatus = "verified"
#: Non-corroborating verdict in the DEFAULT cohort — reported for
#: reground/human follow-up, nothing written (pruning is reground's door).
STATUS_NOT_CORROBORATED: EdgeVerifyStatus = "not-corroborated"
#: Non-corroborating verdict in the ``--unverified-stamped`` cohort — the
#: mint-time ``support`` key is removed (under ``apply``), returning the
#: edge to withheld behind the publish gate. ``verified_by`` is NOT
#: written: a no/contradicts is never stamped.
STATUS_STRIPPED: EdgeVerifyStatus = "stripped"
#: The verify hook returned ``None`` (LLM/dispatch failure) — skipped,
#: retried by a later run; never recorded as a support judgment.
STATUS_LLM_FAILED: EdgeVerifyStatus = "llm-failed"
#: The pinned chunk has no live text (retired/deleted row) — nothing to
#: verify against; repair-evidence territory, like the passage-less rows.
STATUS_CHUNK_MISSING: EdgeVerifyStatus = "chunk-missing"


@dataclass(frozen=True)
class CandidateEdge:
    """One cohort row, carrying everything the verify call needs so
    :func:`verify_edge` does no second lookup: the hub's claim sentence
    (``refs.title``) + scope, the pinned chunk's text/ord, and the source's
    cite key (latest ``ref_identifiers`` ``cite_key``, ``None`` when the
    source has none)."""

    link_id: int
    hub_ref_id: int
    source_ref_id: int
    source_kind: str
    relation: str
    chunk_id: int
    chunk_ord: int | None
    chunk_text: str | None
    cite_key: str | None
    sentence: str
    scope: dict[str, str]


@dataclass(frozen=True)
class EdgeVerifyResult:
    """One edge's outcome. ``applied`` is ``True`` only when a write
    actually ran (a stamp on :data:`STATUS_VERIFIED`, a strip on
    :data:`STATUS_STRIPPED`), so a dry-run row is visibly distinct."""

    link_id: int
    hub_ref_id: int
    source_ref_id: int
    chunk_id: int
    status: EdgeVerifyStatus
    supports: str | None = None
    support_reason: str | None = None
    contradicts: bool = False
    applied: bool = False

    @property
    def action(self) -> str:
        """What was done (``--apply``) or would be (dry-run)."""
        if self.status == STATUS_VERIFIED:
            return "stamped" if self.applied else "would-stamp"
        if self.status == STATUS_STRIPPED:
            return "stripped" if self.applied else "would-strip"
        if self.status == STATUS_NOT_CORROBORATED:
            return "reported"
        return "skipped"

    def to_row(self) -> dict[str, Any]:
        """The JSONL row the CLI writes — proposal (dry-run) or record
        (``--apply``); ``status``/``action`` carry the outcome either way."""
        return {
            "link_id": self.link_id,
            "hub": self.hub_ref_id,
            "source_ref": self.source_ref_id,
            "chunk_id": self.chunk_id,
            "supports": self.supports,
            "support_reason": self.support_reason,
            "contradicts": self.contradicts,
            "status": self.status,
            "action": self.action,
            "applied": self.applied,
        }


# ── cohort selection ────────────────────────────────────────────────────

#: The withheld shape — exactly ``nanopub/preflight.py::withheld_edges``'s
#: predicate (``support`` absent, no human sign-off), minus the passage-less
#: rows the pinned-chunk clause in :data:`_COHORT_SQL` excludes.
_WITHHELD_CLAUSE = """
       AND l.meta->>'support' IS NULL
       AND l.meta->'publish_signoff' IS NULL
"""

#: The born-released shape — a ``support`` stamp nothing ever verified
#: (mint-time default; no ``verified_by`` fingerprint).
_UNVERIFIED_STAMPED_CLAUSE = """
       AND l.meta->>'support' IS NOT NULL
       AND NOT (l.meta ? 'verified_by')
"""

#: Live strict claim hub, alias ``h``. NOT
#: :func:`~precis.taproot.canon.claim_hub_predicate_sql`: that helper
#: deliberately carries no ``rt.expires_at`` filter (the hot dedup path
#: doesn't need it), but a publish-adjacent sweep must not certify edges
#: onto a hub whose claim tag has lapsed — so this copy adds the guard,
#: mirroring ``workers/health_digest.py::_check_nanopub_candidates_fresh``.
#: Bind params: :data:`~precis.taproot.canon.CLAIM_HUB_PREDICATE_PARAMS`.
_LIVE_STRICT_HUB_PREDICATE = """EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = h.ref_id
                AND (rt.expires_at IS NULL OR rt.expires_at > now())
                AND t.namespace = %(taproot_ns)s AND t.value = %(taproot_claim)s
           )
       AND EXISTS (
             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
              WHERE rt.ref_id = h.ref_id
                AND (rt.expires_at IS NULL OR rt.expires_at > now())
                AND t.namespace = %(status_ns)s AND t.value = %(status_canonical)s
           )"""

#: The verifiable cohort. The ``chunks`` join is a LEFT join on purpose: an
#: edge pinned to a retired/deleted chunk row must surface (as
#: ``chunk_text = NULL`` -> :data:`STATUS_CHUNK_MISSING`) rather than drop
#: out of the cohort silently. ``contradicts`` edges are excluded exactly as
#: ``withheld_edges`` excludes them — they block via the contradicts gate,
#: and this sweep must never certify a live dispute.
_COHORT_SQL = """
    SELECT l.link_id, l.dst_ref_id, l.src_ref_id, s.kind, l.relation,
           l.src_chunk_id, c.ord, c.text,
           (SELECT id_value FROM ref_identifiers
             WHERE ref_id = s.ref_id AND id_kind = 'cite_key'
             ORDER BY created_at DESC LIMIT 1) AS cite_key,
           h.title, h.meta->'scope'
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL
      JOIN refs h ON h.ref_id = l.dst_ref_id AND h.retired_at IS NULL
                 AND h.kind = 'finding'
      LEFT JOIN chunks c ON c.chunk_id = l.src_chunk_id
                        AND c.retired_at IS NULL
     WHERE l.relation IN ('establishes', 'corroborates')
       AND l.src_chunk_id IS NOT NULL
       {support_clause}
       AND {hub_predicate}
       {hub_clause}
     ORDER BY l.link_id
     {limit_clause}
"""

#: The rows the pinned-chunk clause excludes — same predicates, no passage.
#: Reported per run so the repair-evidence debt stays visible, never verified.
_PASSAGELESS_COUNT_SQL = """
    SELECT count(*)
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL
      JOIN refs h ON h.ref_id = l.dst_ref_id AND h.retired_at IS NULL
                 AND h.kind = 'finding'
     WHERE l.relation IN ('establishes', 'corroborates')
       AND l.src_chunk_id IS NULL
       {support_clause}
       AND {hub_predicate}
       {hub_clause}
"""


def _select_cohort(
    store: Store,
    support_clause: str,
    *,
    hub_ref_id: int | None = None,
    limit: int | None = None,
) -> list[CandidateEdge]:
    params: dict[str, Any] = dict(CLAIM_HUB_PREDICATE_PARAMS)
    hub_clause = ""
    if hub_ref_id is not None:
        hub_clause = "AND l.dst_ref_id = %(hub)s"
        params["hub"] = hub_ref_id
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %(limit)s"
        params["limit"] = limit
    sql = _COHORT_SQL.format(
        support_clause=support_clause,
        hub_predicate=_LIVE_STRICT_HUB_PREDICATE,
        hub_clause=hub_clause,
        limit_clause=limit_clause,
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        CandidateEdge(
            link_id=int(r[0]),
            hub_ref_id=int(r[1]),
            source_ref_id=int(r[2]),
            source_kind=str(r[3]),
            relation=str(r[4]),
            chunk_id=int(r[5]),
            chunk_ord=int(r[6]) if r[6] is not None else None,
            chunk_text=str(r[7]) if r[7] is not None else None,
            cite_key=str(r[8]) if r[8] else None,
            sentence=str(r[9] or "").strip(),
            scope={str(k): str(v) for k, v in (r[10] or {}).items()},
        )
        for r in rows
    ]


def select_withheld_edges(
    store: Store, *, hub_ref_id: int | None = None, limit: int | None = None
) -> list[CandidateEdge]:
    """The default cohort: pinned, unstamped, un-signed-off evidence edges
    on live strict claim hubs, oldest ``link_id`` first.

    ``hub_ref_id`` scopes to one hub; ``limit`` caps the batch. Both apply
    in SQL, so a limited run is a stable prefix of the full cohort.
    """
    return _select_cohort(store, _WITHHELD_CLAUSE, hub_ref_id=hub_ref_id, limit=limit)


def select_unverified_stamped_edges(
    store: Store, *, hub_ref_id: int | None = None, limit: int | None = None
) -> list[CandidateEdge]:
    """The ``--unverified-stamped`` cohort: pinned edges whose ``support``
    was written at mint time and never verified (no ``verified_by``), on
    live strict claim hubs. Same ordering/scoping as
    :func:`select_withheld_edges`."""
    return _select_cohort(
        store, _UNVERIFIED_STAMPED_CLAUSE, hub_ref_id=hub_ref_id, limit=limit
    )


def count_passageless_edges(
    store: Store, *, unverified_stamped: bool = False, hub_ref_id: int | None = None
) -> int:
    """How many edges the active cohort's predicate matches EXCEPT for the
    passage pin (``src_chunk_id IS NULL``) — skipped, counted, and left to
    ``repair-evidence``/``reground`` (verify needs a passage)."""
    params: dict[str, Any] = dict(CLAIM_HUB_PREDICATE_PARAMS)
    hub_clause = ""
    if hub_ref_id is not None:
        hub_clause = "AND l.dst_ref_id = %(hub)s"
        params["hub"] = hub_ref_id
    sql = _PASSAGELESS_COUNT_SQL.format(
        support_clause=(
            _UNVERIFIED_STAMPED_CLAUSE if unverified_stamped else _WITHHELD_CLAUSE
        ),
        hub_predicate=_LIVE_STRICT_HUB_PREDICATE,
        hub_clause=hub_clause,
    )
    with store.pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None  # count(*) always returns exactly one row
    return int(row[0])


# ── per-edge verify + write ─────────────────────────────────────────────


def _stamp_edge(
    store: Store, link_id: int, verdict: dict[str, Any], sentence: str
) -> None:
    """Write the verdict onto the edge — a jsonb MERGE (``meta || patch``)
    so unrelated keys (``source_handle``, ``origin``, ``char_offset``, …)
    survive. Its own connection context = its own short transaction: a
    crash mid-sweep keeps every edge already stamped."""
    from psycopg.types.json import Jsonb

    patch = {
        "support": verdict.get("supports"),
        "support_reason": verdict.get("support_reason"),
        "caveats": list(verdict.get("caveats") or []),
        "verified_by": VERIFIED_BY,
        "verified_at": datetime.now(UTC).isoformat(),
        "verified_claim_sha": claim_sha(sentence),
    }
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET meta = COALESCE(meta, '{}'::jsonb) || %s "
            "WHERE link_id = %s",
            (Jsonb(patch), link_id),
        )


def _strip_support(store: Store, link_id: int) -> None:
    """Remove exactly the ``support`` key — the edge returns to withheld
    behind the publish gate. Deliberately nothing else: ``verified_by`` is
    not written (a no/contradicts is never stamped), so the edge lands back
    in the default cohort for a later sweep/reground to settle."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET meta = meta - 'support' WHERE link_id = %s",
            (link_id,),
        )


def verify_edge(
    store: Store,
    edge: CandidateEdge,
    *,
    apply: bool = False,
    unverified_stamped: bool = False,
) -> EdgeVerifyResult:
    """Verify ONE edge's pinned passage against its hub's claim and (with
    ``apply=True``) write the outcome.

    ``apply=False`` (the default) computes the verdict and writes NOTHING.
    ``apply=True`` writes only on :data:`STATUS_VERIFIED` (the stamp) or —
    ``unverified_stamped`` runs only — :data:`STATUS_STRIPPED` (``support``
    removed), and only the ``meta`` of the one ``link_id`` passed in. A
    ``None`` verdict (LLM failure) is a skip, never a judgment.
    """
    text = edge.chunk_text
    ord_ = edge.chunk_ord
    if not text or ord_ is None:
        return EdgeVerifyResult(
            link_id=edge.link_id,
            hub_ref_id=edge.hub_ref_id,
            source_ref_id=edge.source_ref_id,
            chunk_id=edge.chunk_id,
            status=STATUS_CHUNK_MISSING,
        )

    verdict = _verify_support_with_caveats(
        claim=edge.sentence,
        scope=dict(edge.scope),
        target_cite_key=edge.cite_key or f"ref:{edge.source_ref_id}",
        target_chunk_ord=ord_,
        target_chunk_text=text,
        source_kind=edge.source_kind,
    )
    if verdict is None:
        return EdgeVerifyResult(
            link_id=edge.link_id,
            hub_ref_id=edge.hub_ref_id,
            source_ref_id=edge.source_ref_id,
            chunk_id=edge.chunk_id,
            status=STATUS_LLM_FAILED,
        )

    supports = verdict.get("supports")
    supports_str = str(supports) if supports is not None else None
    reason = verdict.get("support_reason")
    reason_str = str(reason) if reason is not None else None
    contradicts = bool(verdict.get("contradicts"))

    if is_corroborating(verdict):
        applied = False
        if apply:
            _stamp_edge(store, edge.link_id, verdict, edge.sentence)
            applied = True
        return EdgeVerifyResult(
            link_id=edge.link_id,
            hub_ref_id=edge.hub_ref_id,
            source_ref_id=edge.source_ref_id,
            chunk_id=edge.chunk_id,
            status=STATUS_VERIFIED,
            supports=supports_str,
            support_reason=reason_str,
            contradicts=contradicts,
            applied=applied,
        )

    if unverified_stamped:
        applied = False
        if apply:
            _strip_support(store, edge.link_id)
            applied = True
        return EdgeVerifyResult(
            link_id=edge.link_id,
            hub_ref_id=edge.hub_ref_id,
            source_ref_id=edge.source_ref_id,
            chunk_id=edge.chunk_id,
            status=STATUS_STRIPPED,
            supports=supports_str,
            support_reason=reason_str,
            contradicts=contradicts,
            applied=applied,
        )

    return EdgeVerifyResult(
        link_id=edge.link_id,
        hub_ref_id=edge.hub_ref_id,
        source_ref_id=edge.source_ref_id,
        chunk_id=edge.chunk_id,
        status=STATUS_NOT_CORROBORATED,
        supports=supports_str,
        support_reason=reason_str,
        contradicts=contradicts,
    )
