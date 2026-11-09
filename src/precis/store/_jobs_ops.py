"""Job observability — the measured ``meta.footprint`` rollup.

Mixin on :class:`precis.store.Store`. ``_stamp_job_completion``
(gr595536) stamps every terminal ``kind='job'`` ref with
``meta.footprint = {ru_maxrss_mb, wall_s, cpu_s}`` out of
:mod:`precis.utils.job_footprint`; :meth:`JobsMixin.job_footprint_summary`
folds those samples into the per-``(job_type, meta.size)`` p50/p95 the
footprint-admission table derives from (``docs/backlog/
cluster-scheduling.md`` Pillar 3b: "a high percentile plus margin").
``meta.size`` is the gr595539 mint-time ``{name, value}`` size
parameter — rows minted before it lands group under a null
``size_name``/``size_value`` rather than dropping out of the table.

The ops surface is ``precis stats --job-footprints`` (``cli/stats.py``);
the admission read calls this method directly once Pillar 3b's
fit-then-deficit pull exists.
"""

from __future__ import annotations

from typing import Any


class JobsMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def job_footprint_summary(
        self, *, days: float | None = None
    ) -> list[dict[str, Any]]:
        """Per-``(job_type, meta.size)`` footprint percentiles.

        One dict per key:
        ``{job_type, size_name, size_value, n, rss_p50_mb, rss_p95_mb,
        cpu_p50_s, cpu_p95_s}`` — samples are ``kind='job'`` refs whose
        ``meta.footprint`` carried a measured (non-null) ``ru_maxrss_mb``;
        a null ``cpu_s`` is skipped by ``percentile_cont`` rather than
        dropping the row. ``days`` bounds the sample window on
        ``meta.finished_at``; ``None`` reads all history.
        """
        sql = (
            "SELECT meta->>'job_type' AS job_type, "
            "       meta->'size'->>'name' AS size_name, "
            "       meta->'size'->>'value' AS size_value, "
            "       count(*)::int AS n, "
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY "
            "         (meta->'footprint'->>'ru_maxrss_mb')::float) AS rss_p50_mb, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY "
            "         (meta->'footprint'->>'ru_maxrss_mb')::float) AS rss_p95_mb, "
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY "
            "         (meta->'footprint'->>'cpu_s')::float) AS cpu_p50_s, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY "
            "         (meta->'footprint'->>'cpu_s')::float) AS cpu_p95_s "
            "FROM refs "
            "WHERE kind = 'job' "
            "  AND meta->'footprint'->>'ru_maxrss_mb' IS NOT NULL "
        )
        params: tuple[Any, ...] = ()
        if days is not None:
            sql += (
                "  AND (meta->>'finished_at')::timestamptz "
                "    > now() - (%s || ' days')::interval "
            )
            params = (str(days),)
        sql += "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "job_type": r[0],
                "size_name": r[1],
                "size_value": r[2],
                "n": int(r[3]),
                "rss_p50_mb": round(float(r[4]), 1),
                "rss_p95_mb": round(float(r[5]), 1),
                "cpu_p50_s": round(float(r[6]), 1),
                "cpu_p95_s": round(float(r[7]), 1),
            }
            for r in rows
        ]


__all__ = ["JobsMixin"]
