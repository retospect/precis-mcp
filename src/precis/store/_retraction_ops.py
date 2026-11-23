"""Stored notice analysis and decisions for individual draft blocks."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from psycopg import Connection

from precis.store._secret_scope import secret_fence
from precis.store.core import StoreCore

Verdict = Literal["unaffected", "affects"]


@dataclass(frozen=True, slots=True)
class RetractionNotice:
    id: int
    status: str
    title: str
    date: date | None
    nature: str | None
    analysis: str | None
    analysed_by: str | None
    doi: str | None = None
    source_summary: str | None = None

    @property
    def full_retraction(self) -> bool:
        return (
            self.status == "retracted" and "partial" not in (self.nature or "").lower()
        )


@dataclass(frozen=True, slots=True)
class RetractionVerdict:
    verdict: Verdict
    reason: str
    judged_by: str
    closed: bool
    resolution: str | None = None
    closed_by: str | None = None


class RetractionMixin(StoreCore):
    def retraction_notices(self, paper_id: int) -> list[RetractionNotice]:
        """Include historic notice links whose import predates source indexing."""
        return self.retraction_notices_bulk([paper_id]).get(paper_id, [])

    def retraction_notices_bulk(
        self, paper_ids: Iterable[int]
    ) -> dict[int, list[RetractionNotice]]:
        """:meth:`retraction_notices` for many papers in one query, ``{paper
        id: notices}``; a paper with no notice is absent."""
        ids = sorted(set(paper_ids))
        if not ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT l.src_ref_id, r.ref_id,
                       CASE l.relation WHEN 'retracted-by' THEN 'retracted'
                           WHEN 'corrected-by' THEN 'corrected'
                           ELSE 'expression_of_concern' END,
                       r.title, n.notice_date, nullif(n.nature, ''), n.analysis, n.analysed_by,
                       n.notice_doi, n.source_summary
                   FROM links l
                   JOIN refs r ON r.ref_id = l.dst_ref_id AND r.retired_at IS NULL
                   LEFT JOIN retraction_notices n ON n.notice_id = r.ref_id
                   WHERE l.src_ref_id = ANY(%s)
                     AND l.relation IN ('retracted-by', 'corrected-by', 'concern-raised-by')
                     AND {secret_fence("r", path="retraction-notices")}
                   ORDER BY l.src_ref_id, r.ref_id""",
                (ids,),
            ).fetchall()
        out: dict[int, list[RetractionNotice]] = {}
        for row in rows:
            out.setdefault(int(row[0]), []).append(RetractionNotice(*row[1:]))
        return out

    def retraction_verdicts(
        self, chunk_id: int, text_hash: str
    ) -> dict[tuple[int, int], RetractionVerdict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT paper_id, notice_id, verdict, reason, judged_by, closed_at IS NOT NULL,
                          resolution, closed_by
                   FROM retraction_use_verdicts WHERE chunk_id = %s AND text_hash = %s""",
                (chunk_id, text_hash),
            ).fetchall()
        return {(row[0], row[1]): RetractionVerdict(*row[2:]) for row in rows}

    def analyse_retraction(self, notice_id: int, analysis: str, actor: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO retraction_notices (notice_id, nature, analysis, analysed_by)
                   VALUES (%s, '', %s, %s)
                   ON CONFLICT (notice_id) DO UPDATE
                   SET analysis = EXCLUDED.analysis, analysed_by = EXCLUDED.analysed_by
                   WHERE retraction_notices.analysis IS NULL""",
                (notice_id, analysis, actor),
            )

    def judge_retraction_use(
        self,
        notice_id: int,
        paper_id: int,
        chunk_id: int,
        text_hash: str,
        verdict: Verdict,
        reason: str,
        actor: str,
        *,
        conn: Connection | None = None,
    ) -> bool:
        """Record a decision against the currently read block revision."""
        if conn is None:
            with self.pool.connection() as own_conn:
                return self.judge_retraction_use(
                    notice_id,
                    paper_id,
                    chunk_id,
                    text_hash,
                    verdict,
                    reason,
                    actor,
                    conn=own_conn,
                )
        row = conn.execute(
            """INSERT INTO retraction_use_verdicts
                       (notice_id, paper_id, chunk_id, text_hash, verdict, reason, judged_by, closed_at)
                   SELECT n.notice_id, %s, %s, %s, %s, %s, %s,
                          CASE WHEN %s = 'unaffected' THEN now() END
                   FROM retraction_notices n
                   JOIN chunks c ON c.chunk_id = %s AND c.content_sha = %s
                   JOIN links l ON l.dst_ref_id = n.notice_id AND l.src_ref_id = %s
                     AND l.relation IN ('retracted-by', 'corrected-by', 'concern-raised-by')
                   WHERE n.notice_id = %s AND n.analysis IS NOT NULL AND c.retired_at IS NULL
                   LIMIT 1
                   ON CONFLICT (chunk_id, paper_id, notice_id, text_hash) DO NOTHING
                   RETURNING notice_id""",
            (
                paper_id,
                chunk_id,
                text_hash,
                verdict,
                reason,
                actor,
                verdict,
                chunk_id,
                text_hash,
                paper_id,
                notice_id,
            ),
        ).fetchone()
        return row is not None

    def close_retraction_use(
        self,
        notice_id: int,
        paper_id: int,
        chunk_id: int,
        text_hash: str,
        resolution: str,
        actor: str,
    ) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE retraction_use_verdicts
                   SET closed_at = now(), resolution = %s, closed_by = %s
                   WHERE notice_id = %s AND paper_id = %s AND chunk_id = %s
                     AND text_hash = %s AND closed_at IS NULL AND verdict = 'affects'
                   RETURNING notice_id""",
                (resolution, actor, notice_id, paper_id, chunk_id, text_hash),
            ).fetchone()
        return row is not None
