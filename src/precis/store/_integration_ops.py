"""The integration ledger — paper-writing pipeline rung 2 (docs/backlog/paper-writing-pipeline.md §"The integration ledger"). Mixin on
:class:`precis.store.Store`.

``integrated-into`` is not a new table: disposition rides the existing
refs↔refs ``links`` edge as its relation. A disposition edge is always
``paper --relation--> dossier draft`` (``src_ref_id``=paper,
``dst_ref_id``=dossier), with an optional section anchor as
``dst_chunk_id`` (the section heading chunk a caller targets via the
standard ``draft:<slug>~<selector>`` grammar — see
``handlers/_link_target.py``). The four disposition relations
(``cited-in`` / ``corroborates`` / ``superseded-in`` / ``off-topic-for``,
migration 0085) are asymmetric with **no** inverse, so dossier→papers
traversal is a plain inbound query, not ``links_for``'s inverse-mirror
machinery — hence the two dedicated queries here rather than reuse of
``LinksMixin.links_for``.

Two read-only queries back ``view='integration'``
(:mod:`precis.handlers._integration_view`):

* :meth:`IntegrationLedgerMixin.integration_ledger` — every disposition
  edge landed on a dossier (the INTEGRATED side).
* :meth:`IntegrationLedgerMixin.unintegrated_papers` — the minus-query:
  refs tagged ``topic:<t>`` (any of the dossier's topics) with **no**
  disposition edge to it yet (the PENDING side; the "unintegrated for X"
  live query, §"The relevance question — resolved").
"""

from __future__ import annotations

from typing import Any

from psycopg_pool import ConnectionPool

from precis.store._tag_filter import build_tag_filter

#: The four disposition relations (migration 0085). Kept local rather
#: than imported from ``store.types.Relation`` (a bare ``Literal`` isn't
#: iterable) — this is the runtime list used to build ``= ANY(%s))``
#: clauses; the DB `relations` table + the `Relation` Literal are the
#: vocabulary authorities.
DISPOSITION_RELATIONS: tuple[str, ...] = (
    "cited-in",
    "corroborates",
    "superseded-in",
    "off-topic-for",
)


class IntegrationLedgerMixin:
    """Disposition-edge queries for ``view='integration'``."""

    pool: ConnectionPool

    def integration_ledger(self, dossier_ref_id: int) -> list[dict[str, Any]]:
        """Every inbound disposition edge on ``dossier_ref_id`` (a draft).

        One row per edge:

        * ``paper_ref_id`` / ``paper_title`` — the citing paper.
        * ``section_chunk_id`` — the anchored section heading chunk's id
          (``links.dst_chunk_id``), or ``None`` for a whole-document edge.
        * ``section_heading`` — that chunk's text, or ``None``.
        * ``relation`` — which of the four dispositions.
        * ``at`` — ``links.created_at``.

        Ordered oldest-first (mirrors ``LinksMixin.links_for``).
        """
        sql = (
            "SELECT DISTINCT l.link_id, l.src_ref_id AS paper_ref_id, "
            "r.title AS paper_title, l.dst_chunk_id AS section_chunk_id, "
            "c.text AS section_heading, l.relation, l.created_at AS at "
            "FROM links l "
            "JOIN refs r ON r.ref_id = l.src_ref_id AND r.retired_at IS NULL "
            "LEFT JOIN chunks c ON c.chunk_id = l.dst_chunk_id "
            "WHERE l.dst_ref_id = %s AND l.relation = ANY(%s) "
            "ORDER BY l.created_at ASC"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(
                sql, (dossier_ref_id, list(DISPOSITION_RELATIONS))
            ).fetchall()
        return [
            {
                "paper_ref_id": r[1],
                "paper_title": r[2],
                "section_chunk_id": r[3],
                "section_heading": r[4],
                "relation": r[5],
                "at": r[6],
            }
            for r in rows
        ]

    def unintegrated_papers(
        self, dossier_ref_id: int, topics: list[str]
    ) -> list[dict[str, Any]]:
        """Papers tagged ``topic:<t>`` (any ``t`` in ``topics``) that carry
        **no** disposition edge to ``dossier_ref_id`` yet — the weekly gap
        review's minus-query (§"The integration ledger": ``topic:X`` minus
        ``integrated-into``).

        Each ``topic:<t>`` leg is built with
        :func:`precis.store._tag_filter.build_tag_filter` (its AND
        semantics fit a *single*-tag list); the legs are OR-ed together
        since a paper need only carry *one* of the dossier's topics.
        Returns ``[]`` for an empty ``topics`` — no topics means no
        pending set, not "everything."
        """
        if not topics:
            return []

        frags: list[str] = []
        params: list[Any] = []
        for t in topics:
            frag, frag_params = build_tag_filter([f"topic:{t}"], ref_alias="r")
            if frag:
                frags.append(frag)
                params.extend(frag_params)
        if not frags:
            return []
        tag_clause = "(" + " OR ".join(frags) + ")"

        sql = (
            "SELECT r.ref_id AS paper_ref_id, r.title AS title "
            "FROM refs r "
            "WHERE r.retired_at IS NULL AND r.kind = 'paper' "
            f"AND {tag_clause} "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM links l "
            "  WHERE l.src_ref_id = r.ref_id AND l.dst_ref_id = %s "
            "    AND l.relation = ANY(%s)"
            ") "
            "ORDER BY r.ref_id ASC"
        )
        params.extend([dossier_ref_id, list(DISPOSITION_RELATIONS)])

        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"paper_ref_id": r[0], "title": r[1]} for r in rows]


__all__ = ["DISPOSITION_RELATIONS", "IntegrationLedgerMixin"]
