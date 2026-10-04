"""Grounding freshness at sign — did supporting evidence arrive after the
grounding froze?

Approval freezes the grounding envelope (``nanopub_publish.grounding``).
Evidence edges attached to the hub afterwards are not in it, and nothing
on the sign path used to say so: fi189535 was approved with an abstract +
a definition, its TEM caption and STS passages were linked as
``establishes`` evidence 23 minutes later, and it was signed three days on
without anyone seeing them. :func:`stale_grounding` lists those edges;
:func:`check_grounding_fresh` turns them into the ``grounding-stale`` gate
violation that :func:`precis.nanopub.mint.sign` raises unless the caller
passes ``accept_newer_evidence`` (a person looked and signed anyway).

The freeze time lives in the envelope as ``frozen_at`` (UTC ISO-8601 'Z',
stamped by :meth:`precis.store.Store.nanopub_approve`, the one write
``mint.approve`` freezes through, from the same DB ``now()`` that sets
``updated_at`` and ``links.created_at`` — no app/DB clock skew; no
migration). Every successful sign also stamps ``checked_at`` (same
format and clock, in :meth:`precis.store.Store.nanopub_record_signed`'s own
UPDATE): at that moment each supporting edge was either in the grounding
or confirmed by the signer, so the threshold is
``max(frozen_at, checked_at)`` (:func:`grounded_since`) and a signed →
reviewed dependency re-mint only flags edges newer than the last sign —
re-signing is never blocked by edges already confirmed. Neither stamp is
part of the artifact: :func:`precis.nanopub.mint._mint_input` reads named
envelope keys only (``passages`` / ``fields`` / ``motivation`` /
``testable_by``), and the gates read the same named keys, so an extra key
changes neither the signed bytes nor ``claim_sha`` (which hashes the
approved title alone); ``checked_at`` is written after the artifact is
minted. Edges pinned to a retired chunk are ignored. Rows approved before it shipped fall back to the
publish row's ``updated_at`` — set by ``approve`` and, for a ``reviewed``
row, bumped by nothing else except the dependency-drift flip
(``signed`` → ``reviewed``, :func:`precis.nanopub.mint.check_dependency_drift`),
which re-dates a legacy row to the flip: evidence between the original
freeze and the flip is then missed for that legacy row, never over-reported.
New rows keep their original ``frozen_at`` (and ``checked_at``) through
that flip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.nanopub import evidence
from precis.nanopub.gates import GateViolation, integral_chunk_id
from precis.nanopub.preflight import _grounding_chunk_id
from precis.store._nanopub_ops import PublishRow
from precis.taproot.hub import EVIDENCE_SRC_KINDS, PATHWAY_EVIDENCE_KINDS
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store

#: Envelope key holding the freeze time.
FROZEN_AT_KEY = "frozen_at"

#: Envelope key holding the last successful sign time.
CHECKED_AT_KEY = "checked_at"

#: Gate slug for the sign-time refusal (and the preflight check name).
GATE = "grounding-stale"

#: Evidence relations that SUPPORT the claim (``taproot.hub.HUB_ROLES``
#: minus ``contradicts``). A ``contradicts`` edge blocks via its own gate;
#: a ``disputes`` edge is a non-blocking open question — neither is
#: "evidence the reviewer should have seen before attesting".
SUPPORT_RELATIONS = ("establishes", "corroborates")

#: How many edges the refusal message lists before "+N more".
_LIST_CAP = 10

_SRC_KINDS = sorted(EVIDENCE_SRC_KINDS | PATHWAY_EVIDENCE_KINDS)


@dataclass(frozen=True, slots=True)
class NewerEdge:
    """One supporting evidence edge that arrived after the freeze and is
    not part of the frozen grounding."""

    link_id: int
    relation: str
    #: Source's ``cite_key`` when it has one, else its handle (``pa<id>``).
    source: str
    source_title: str
    paper_ref_id: int
    #: ``pc<id>`` of the passage the edge pins (``None`` = paper-level).
    chunk_handle: str | None
    chunk_id: int | None
    created_at: datetime

    def describe(self) -> str:
        where = f" {self.chunk_handle}" if self.chunk_handle else " (paper-level)"
        return (
            f"link {self.link_id} {self.relation} ← {self.source}{where} "
            f"({self.created_at.strftime('%Y-%m-%dT%H:%MZ')})"
        )


def _stamp(row: PublishRow, key: str) -> datetime | None:
    raw = (row.grounding or {}).get(key)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def frozen_at(row: PublishRow) -> datetime:
    """When the row's grounding froze: the envelope's ``frozen_at``, else
    the row's ``updated_at`` (rows approved before the stamp existed —
    see the module docstring for what can move it)."""
    return _stamp(row, FROZEN_AT_KEY) or row.updated_at


def grounded_since(row: PublishRow) -> datetime:
    """The threshold :func:`stale_grounding` compares edges against:
    ``max(frozen_at, checked_at)``. ``checked_at`` is stamped on every
    successful sign, so after a signed → reviewed re-mint only edges newer
    than the last sign (which the signer had to see or confirm) count."""
    frozen = frozen_at(row)
    checked = _stamp(row, CHECKED_AT_KEY)
    return max(frozen, checked) if checked else frozen


def stale_grounding(store: Store, hub_ref_id: int, row: PublishRow) -> list[NewerEdge]:
    """Live supporting ``source → hub`` edges created after ``row``'s
    grounding froze whose passage (or, for a paper-level edge, whose
    source) is not in the frozen grounding. ``establishes`` first, then
    oldest first. Pure read; ``[]`` for a row with no frozen grounding."""
    if not row.grounding:
        return []
    frozen = grounded_since(row)
    passages = list((row.grounding or {}).get("passages") or [])
    grounded_chunks = {
        cid
        for cid in (integral_chunk_id(p.get("chunk_id")) for p in passages)
        if cid is not None
    }
    grounded_refs = {
        c.ref_id for c in evidence.fetch_chunks(store, sorted(grounded_chunks))
    }

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.link_id, l.relation, l.src_ref_id, l.src_chunk_id, l.meta,
                   l.created_at, r.title
              FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id AND r.retired_at IS NULL
             WHERE l.dst_ref_id = %(hub)s
               AND l.relation = ANY(%(rels)s)
               AND r.kind = ANY(%(kinds)s)
               AND l.created_at > %(frozen)s
             ORDER BY (l.relation = 'establishes') DESC, l.created_at, l.link_id
            """,
            {
                "hub": hub_ref_id,
                "rels": list(SUPPORT_RELATIONS),
                "kinds": _SRC_KINDS,
                "frozen": frozen,
            },
        ).fetchall()

    # A retired chunk (a re-chunk's soft-deleted leftover) is no passage a
    # reviewer could read; an edge pinned to one is skipped. (Retired
    # *grounding* chunks still resolve in `grounded_refs` above:
    # `fetch_chunks` does not filter on `retired_at`, so a re-chunk never
    # flags the paper it already quotes.)
    pinned = sorted(
        {
            cid
            for cid in (_grounding_chunk_id(r[3], r[4] or {}) for r in rows)
            if cid is not None
        }
    )
    retired: set[int] = set()
    if pinned:
        with store.pool.connection() as conn:
            retired = {
                int(x[0])
                for x in conn.execute(
                    "SELECT chunk_id FROM chunks "
                    "WHERE chunk_id = ANY(%s) AND retired_at IS NOT NULL",
                    (pinned,),
                ).fetchall()
            }

    picked: list[tuple[Any, int | None]] = []
    for r in rows:
        chunk_id = _grounding_chunk_id(r[3], r[4] or {})
        if chunk_id in retired:
            continue
        if chunk_id is not None:
            if chunk_id in grounded_chunks:
                continue
        elif int(r[2]) in grounded_refs:
            continue  # paper-level edge from a source the grounding quotes
        picked.append((r, chunk_id))
    if not picked:
        return []

    ids_by_ref = store.identifiers_for_refs(sorted({int(r[2]) for r, _ in picked}))
    refs = store.fetch_refs_by_ids({int(r[2]) for r, _ in picked})
    edges: list[NewerEdge] = []
    for r, chunk_id in picked:
        ref_id = int(r[2])
        ref = refs.get(ref_id)
        kind = ref.kind if ref is not None else "paper"
        handle = handle_registry.try_format(kind, ref_id) or f"ref:{ref_id}"
        edges.append(
            NewerEdge(
                link_id=int(r[0]),
                relation=str(r[1]),
                source=ids_by_ref.get(ref_id, {}).get("cite_key") or handle,
                source_title=str(r[6] or ""),
                paper_ref_id=ref_id,
                chunk_handle=(
                    handle_registry.try_format(kind, chunk_id, chunk=True)
                    if chunk_id is not None
                    else None
                ),
                chunk_id=chunk_id,
                created_at=r[5],
            )
        )
    return edges


def format_edges(edges: list[NewerEdge], *, cap: int = _LIST_CAP) -> str:
    """The edge list for a refusal/preflight message: capped, "+N more"."""
    shown = "; ".join(e.describe() for e in edges[:cap])
    more = len(edges) - cap
    return shown + (f"; +{more} more" if more > 0 else "")


def grounding_stale_message(hub_ref_id: int, edges: list[NewerEdge]) -> str:
    return (
        f"{len(edges)} supporting evidence edge(s) arrived after the grounding "
        f"froze and are not in it: {format_edges(edges)}. Either re-review "
        f"(reopen to candidate and re-approve: `precis nanopub reopen "
        f"fi{hub_ref_id}`, then approve) or, having read them, sign anyway "
        "(`--accept-newer-evidence` on the CLI; the checkbox on the claim page)."
    )


def check_grounding_fresh(
    store: Store, hub_ref_id: int, row: PublishRow
) -> tuple[GateViolation | None, list[NewerEdge]]:
    """The sign-time gate: ``(violation, edges)`` — ``violation`` is
    ``None`` when nothing newer arrived."""
    edges = stale_grounding(store, hub_ref_id, row)
    if not edges:
        return None, []
    return GateViolation(GATE, grounding_stale_message(hub_ref_id, edges)), edges
