"""Flag paper bodies that address pages outside their canonical PDF.

Blank or omitted pages can leave sparse extraction, so only out-of-range
labels count as evidence. The same check runs at ingest and in the
``precis paper flag-page-mismatches`` metadata-only corpus scan.

When using a flagged paper, supply the complete PDF of the same version
through ``paper_pull``, then explicitly re-extract from the new attached
SHA. That pass stores independent page-image table readings with extractor
and model provenance alongside the replacement body. Review dependants
after the swap and supersede any signed findings that need correction.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg import Connection


PAGE_SPAN_RE = re.compile(r'<span\s+id="page-([0-9]{1,10})-[0-9]+"\s*>')


def stamp_page_mismatches(conn: Connection, first: int, last: int) -> int:
    """Stamp out-of-range zero-based page labels without changing the body.

    Sparse and unpaginated bodies are valid. Inspect the Marker spans as
    well as chunk columns, which can hold an approximate page assignment.
    """
    return conn.execute(
        r"""
        WITH mismatches AS (
            SELECT r.ref_id, r.pdf_sha256,
                   jsonb_build_object(
                       'pdf_sha256', r.pdf_sha256,
                       'pdf_page_count', p.page_count,
                       'last_body_page', labels.last_page
                   ) AS evidence
              FROM refs r JOIN pdfs p USING (pdf_sha256)
              CROSS JOIN LATERAL (
                  SELECT max(greatest(c.page_first, c.page_last, spans.last_page))
                         AS last_page
                    FROM chunks c
                    LEFT JOIN LATERAL (
                        SELECT max(m[1]::bigint) AS last_page
                          FROM regexp_matches(c.text,
                              '<span[[:space:]]+id="page-([0-9]{1,10})-[0-9]+"[[:space:]]*>',
                              'g') AS m
                    ) spans ON true
                   WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
              ) labels
             WHERE r.ref_id BETWEEN %s AND %s AND r.kind = 'paper'
               AND r.retired_at IS NULL AND p.page_count > 0
               AND r.meta->'needs_refinement' IS NULL
               AND labels.last_page >= p.page_count
        )
        UPDATE refs r
           SET meta = jsonb_set(coalesce(r.meta, '{}'::jsonb),
                               '{needs_refinement}', m.evidence),
               updated_at = now()
          FROM mismatches m
         WHERE r.ref_id = m.ref_id AND r.retired_at IS NULL
           AND r.pdf_sha256 = m.pdf_sha256
           AND r.meta->'needs_refinement' IS NULL
        """,
        (first, last),
    ).rowcount
