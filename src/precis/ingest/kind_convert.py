"""``paper`` → ``datasheet`` kind conversion (gr515477).

Datasheets and manuals ingested through the paper pipeline land as
``kind='paper'`` — with junk authors, junk-title flags and academic
search pollution. The convert is a ``refs.kind`` flip, not a body move:
every ref-scoped table (``chunks`` + derived rows, ``links`` including
chunk anchors, ``ref_identifiers``, ``paper_authors``,
``paper_bib_entries``, ``ref_events``) is keyed by ``ref_id`` and
follows the same row untouched — the least-destructive path.

The one denormalised copy is ``chunk_embeddings.kind`` (migration
0226): its ``(ref_id, kind) -> refs`` FK is ``ON UPDATE CASCADE``, so
the refs flip re-kinds the already-backfilled rows itself; the explicit
UPDATE after it re-stamps the rows the prod backfill hasn't reached —
``ref_id`` is still NULL there, so it re-derives both columns through
the chunk join rather than filtering on ``ref_id`` alone. (On prod the
filled-check constraint is NOT VALID while the backfill is pending;
rows like that can't exist on a validated dev/test schema.)

Trust stamps live on the ``refs`` row itself, but a kind flip is a
*semantic* change under the 0214 guard: a v0 row keeps
``unknown/unknown``; a v1 row (``trust_write_version = 1``) is
restamped ``<caller origin>/hunch`` with a recorded act — the door runs
under ``trust_door('computed')`` but an MCP verb's own producer wins,
so a ``considered`` stamp does not survive. The dry run names this
when the ref is v1.

Refusals (a datasheet can't hold these):

- an SI record (``pdf_role='supplement'`` or ``meta.si_parent``) — its
  cite-as-parent machinery and ``si_fetch`` queue are paper-only;
- a paper with live SI children — converting the parent strands them;
- links whose relation's ``domain_kinds`` / ``range_kinds``
  (migration 0180) exclude the target kind — the edge would violate
  the constraint it was written under.

Paper-only ref tags are stripped in the same transaction (the axis
vocabulary's categorizer axes applicable to ``paper`` but not the
target, each stripped axis's ``<NS>CASCADE`` sweep marker, plus
``DREAM`` — the dream-acquire provenance stamp): the tag
gate's per-kind axis whitelist doesn't admit them on a datasheet.
Open (``OPEN``-namespace) tags and multi-kind axes (``SRC``/``CACHE``)
stay.

Handles are computed from ``(kind, ref_id)``: after conversion the
record answers ``da<id>``/``dk<chunk>`` — ``pa<id>``/``pc<chunk>`` keep
resolving through the ``meta.converted_from_kind`` redirect in
``Store.resolve_handle``. The cite_key slug and every
``ref_identifiers`` alias (DOI, arXiv, pdf_sha256…) keep resolving
under the new kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from precis.store.revision_context import trust_door

#: The electronics-doc sub-genres a datasheet's ``meta.subtype`` may
#: name — ``datasheet`` is the default. Shared by the datasheet door's
#: ``subtype=`` field (handlers/datasheet.py) and the convert door.
DATASHEET_SUBTYPES: frozenset[str] = frozenset(
    {"datasheet", "app-note", "errata", "reference-manual"}
)

#: Kinds a paper may convert to. One today — the tuple is the door's
#: refusal vocabulary if a second target lands.
CONVERT_TARGETS: frozenset[str] = frozenset({"datasheet"})


def paper_only_tag_namespaces(target: str) -> frozenset[str]:
    """Namespaces whose ref tags a ``target``-kind ref can't hold after
    convert: every categorizer axis (``data/axes/*.yaml``) applicable
    to ``paper`` but not ``target``, each such axis's ``<NS>CASCADE``
    sweep marker (stale bookkeeping on a kind the axis can't tag),
    plus ``DREAM`` (the dream-acquire provenance stamp — paper stubs
    only)."""
    from precis.store.types import _CATEGORIZER_AXES

    axes = {
        ns
        for ns, ax in _CATEGORIZER_AXES.items()
        if "paper" in ax.kinds and target not in ax.kinds
    }
    return frozenset(axes | {f"{ns}CASCADE" for ns in axes} | {"DREAM"})


@dataclass(frozen=True)
class ConvertReport:
    """What a convert did — or, from :func:`probe_convert`, would do.

    ``embedding_rows`` counts ``chunk_embeddings`` rows on the ref's
    chunks the kind flip re-stamps (counted through the chunk join —
    prod rows still awaiting the 0226 backfill have ``ref_id`` NULL and
    a ``ref_id = X`` filter would miss them). ``links`` counts
    ref-anchored edges, ``chunk_links`` the chunk-anchored subset —
    all carried, none moved (the ``ref_id`` doesn't change).
    ``stripped_tags`` lists ``NS:value`` ref tags the target can't
    hold. ``trust_write_version`` feeds the dry run's restamp warning.
    ``violated_links`` is ``(link_id, relation, 'domain'|'range')`` —
    the refusals the door reports. ``is_supplement`` / ``si_children``
    are the other two refuse flags.
    """

    from_kind: str = "paper"
    to_kind: str = "datasheet"
    chunks: int = 0
    retired_chunks: int = 0
    embedding_rows: int = 0
    links: int = 0
    chunk_links: int = 0
    identifiers: int = 0
    authors: int = 0
    pdf: bool = False
    trust_write_version: int = 0
    stripped_tags: list[str] = field(default_factory=list)
    violated_links: list[tuple[int, str, str]] = field(default_factory=list)
    is_supplement: bool = False
    si_children: int = 0


def probe_convert(conn: Any, ref_id: int, target: str) -> ConvertReport:
    """Read-only snapshot of what a kind flip touches (dry-run probe).

    The caller resolves and liveness-checks the ref first — this reads
    counts only, plus the three refuse flags the door turns into
    ``BadInput`` with ``next=``.
    """
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE retired_at IS NULL), "
        "       count(*) FILTER (WHERE retired_at IS NOT NULL) "
        "FROM chunks WHERE ref_id = %s AND ord >= 0",
        (ref_id,),
    ).fetchone()
    chunks = int(row[0]) if row else 0
    retired = int(row[1]) if row else 0
    row = conn.execute(
        "SELECT pdf_role, meta ? 'si_parent', pdf_sha256 IS NOT NULL, "
        "       trust_write_version "
        "FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    is_si = bool(row and (row[0] == "supplement" or row[1]))
    pdf = bool(row and row[2])
    trust_version = int(row[3]) if row else 0
    # Count through the chunk join: unfilled 0226 rows have ref_id NULL.
    embedding_rows = int(
        conn.execute(
            "SELECT count(*) FROM chunk_embeddings e "
            "JOIN chunks c ON c.chunk_id = e.chunk_id "
            "WHERE c.ref_id = %s",
            (ref_id,),
        ).fetchone()[0]
    )
    links = int(
        conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (ref_id, ref_id),
        ).fetchone()[0]
    )
    chunk_links = int(
        conn.execute(
            "SELECT count(DISTINCT l.link_id) FROM links l "
            "JOIN chunks c ON c.chunk_id = l.src_chunk_id "
            "            OR c.chunk_id = l.dst_chunk_id "
            "WHERE c.ref_id = %s AND c.ord >= 0",
            (ref_id,),
        ).fetchone()[0]
    )
    identifiers = int(
        conn.execute(
            "SELECT count(*) FROM ref_identifiers WHERE ref_id = %s", (ref_id,)
        ).fetchone()[0]
    )
    authors = int(
        conn.execute(
            "SELECT count(*) FROM paper_authors WHERE ref_id = %s", (ref_id,)
        ).fetchone()[0]
    )
    si_children = int(
        conn.execute(
            "SELECT count(*) FROM links l "
            "JOIN refs s ON s.ref_id = l.src_ref_id "
            "           AND s.retired_at IS NULL "
            "           AND s.pdf_role = 'supplement' "
            "WHERE l.dst_ref_id = %s AND l.relation = 'part-of' "
            "  AND l.meta->>'role' = 'supplement'",
            (ref_id,),
        ).fetchone()[0]
    )
    stripped = [
        f"{r[0]}:{r[1]}"
        for r in conn.execute(
            "SELECT t.namespace, t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = ANY(%s) "
            "ORDER BY t.namespace, t.value",
            (ref_id, sorted(paper_only_tag_namespaces(target))),
        ).fetchall()
    ]
    # Endpoint-kind constraints (migration 0180): a link whose src is
    # this ref must accept the target in domain_kinds, a dst in
    # range_kinds. NULL means unconstrained.
    violated = [
        (int(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT l.link_id, l.relation, "
            "       CASE WHEN l.src_ref_id = %(r)s "
            "            THEN 'domain' ELSE 'range' END "
            "FROM links l JOIN relations r2 ON r2.slug = l.relation "
            "WHERE (l.src_ref_id = %(r)s AND r2.domain_kinds IS NOT NULL "
            "       AND NOT (%(t)s = ANY(r2.domain_kinds))) "
            "   OR (l.dst_ref_id = %(r)s AND r2.range_kinds IS NOT NULL "
            "       AND NOT (%(t)s = ANY(r2.range_kinds))) "
            "ORDER BY l.link_id",
            {"r": ref_id, "t": target},
        ).fetchall()
    ]
    return ConvertReport(
        to_kind=target,
        chunks=chunks,
        retired_chunks=retired,
        embedding_rows=embedding_rows,
        links=links,
        chunk_links=chunk_links,
        identifiers=identifiers,
        authors=authors,
        pdf=pdf,
        trust_write_version=trust_version,
        stripped_tags=stripped,
        violated_links=violated,
        is_supplement=is_si,
        si_children=si_children,
    )


@trust_door("computed")
def apply_convert(
    conn: Any, *, ref_id: int, target: str, subtype: str
) -> dict[str, Any]:
    """Flip ``refs.kind`` to ``target``, restamp the denormalised
    ``chunk_embeddings.kind`` and strip paper-only ref tags. Returns
    ``{"embedding_rows": n, "stripped_tags": [...]}`` for the event
    payload.

    Under the ``computed`` door like the body ops: a version-1 ref's
    semantic update (kind IS semantic) restamps through the 0214 guard
    with a recorded act instead of refusing. Runs inside the caller's
    transaction — the door's TOCTOU re-check pins ``kind='paper'``
    before this runs.
    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        "UPDATE refs SET kind = %s, "
        "meta = meta || %s::jsonb, updated_at = now() "
        "WHERE ref_id = %s AND kind = 'paper'",
        (
            target,
            Jsonb(
                {
                    "subtype": subtype,
                    "converted_from_kind": "paper",
                    "converted_at": stamp,
                }
            ),
            ref_id,
        ),
    )
    # The composite FK cascades the parent's kind into already-filled
    # rows; this catches the rest — prod rows still unfilled by the
    # 0226 backfill have ref_id NULL, so re-derive both columns
    # through the chunk join rather than trusting e.ref_id.
    cur = conn.execute(
        "UPDATE chunk_embeddings e SET ref_id = c.ref_id, kind = %s "
        "FROM chunks c "
        "WHERE e.chunk_id = c.chunk_id AND c.ref_id = %s "
        "  AND (e.ref_id IS DISTINCT FROM c.ref_id "
        "       OR e.kind IS DISTINCT FROM %s)",
        (target, ref_id, target),
    )
    stripped = [
        f"{r[0]}:{r[1]}"
        for r in conn.execute(
            "DELETE FROM ref_tags rt USING tags t "
            "WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "  AND t.namespace = ANY(%s) "
            "RETURNING t.namespace, t.value",
            (ref_id, sorted(paper_only_tag_namespaces(target))),
        ).fetchall()
    ]
    return {
        "embedding_rows": max(cur.rowcount, 0),
        "stripped_tags": sorted(stripped),
    }
