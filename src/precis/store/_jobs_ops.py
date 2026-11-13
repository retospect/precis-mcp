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

gr595542 adds the two pull-side reads over this data plus the
per-model VRAM table:

* :func:`predicted_rss_mb` — the conn-based p95 read the claim path
  (:mod:`precis.workers.footprint_admission`) reserves ``mem_mb``
  against, exact ``(job_type, meta.size)`` key, dark below a minimum
  sample count;
* :func:`model_footprint_vram_mb` + the ``model_footprint`` table
  (migration 0322) — one row per model (weights + KV for the context
  length, learnable from a single run), the GPU-admission read.

The ops surface is ``precis stats --job-footprints`` /
``--model-footprints`` (``cli/stats.py``).
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection


def predicted_rss_mb(
    conn: Connection,
    job_type: str,
    size_name: str | None,
    size_value: str | None,
    *,
    min_samples: int,
) -> float | None:
    """The p95 measured peak RSS for one exact ``(job_type, meta.size)``
    footprint key, or ``None`` when the key has too few samples to
    admit against (gr595542).

    The claim path's CPU admission reserves ``mem_mb`` units off this
    number (times a margin the caller applies), inside the claim
    transaction — same keying and null-handling as
    :meth:`JobsMixin.job_footprint_summary`, minus the ``days`` window
    (the rollup row count is small; admission wants all history). A job
    whose type (or exact size bucket) hasn't terminalised
    ``min_samples`` measured runs reads ``None``: the claim then
    carries no memory reservation rather than a made-up number.
    """
    row = conn.execute(
        "SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY "
        "         (meta->'footprint'->>'ru_maxrss_mb')::float) AS p95, "
        "       count(*)::int AS n "
        "  FROM refs "
        " WHERE kind = 'job' "
        "   AND meta->>'job_type' = %s "
        "   AND meta->'footprint'->>'ru_maxrss_mb' IS NOT NULL "
        "   AND meta->'size'->>'name' IS NOT DISTINCT FROM %s "
        "   AND meta->'size'->>'value' IS NOT DISTINCT FROM %s",
        (job_type, size_name, size_value),
    ).fetchone()
    if row is None or int(row[1]) < min_samples or row[0] is None:
        return None
    return float(row[0])


def model_footprint_vram_mb(conn: Connection, model: str) -> int | None:
    """The learned VRAM footprint (weights + KV) for ``model``, or
    ``None`` when the ``model_footprint`` table (0322) has no row —
    the GPU admission is dark for unlearned models."""

    row = conn.execute(
        "SELECT vram_mb FROM model_footprint WHERE model = %s", (model,)
    ).fetchone()
    return int(row[0]) if row is not None else None


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

    def model_footprints(self) -> list[dict[str, Any]]:
        """Every ``model_footprint`` row (gr595542), oldest first —
        the ``precis stats --model-footprints`` listing."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT model, vram_mb, context_tokens, learned_at, source "
                "  FROM model_footprint ORDER BY learned_at, model"
            ).fetchall()
        return [
            {
                "model": str(r[0]),
                "vram_mb": int(r[1]),
                "context_tokens": int(r[2]) if r[2] is not None else None,
                "learned_at": r[3].isoformat() if r[3] is not None else None,
                "source": str(r[4]),
            }
            for r in rows
        ]

    def upsert_model_footprint(
        self,
        model: str,
        vram_mb: int,
        *,
        context_tokens: int | None = None,
        source: str = "",
    ) -> None:
        """Record (or replace) one model's learned VRAM footprint.

        The learner side of the gr595542 GPU admission: a single
        measured run pins a model's weights + KV footprint, so the
        write replaces the row wholesale (``learned_at`` refreshes) —
        no percentile folding like the RSS rollup. Per-run VRAM
        measurement (the automatic caller) is gr595537+; until then
        operators and tests seed rows through here.
        """
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO model_footprint "
                    "(model, vram_mb, context_tokens, learned_at, source) "
                    "VALUES (%s, %s, %s, now(), %s) "
                    "ON CONFLICT (model) DO UPDATE SET "
                    "  vram_mb = EXCLUDED.vram_mb, "
                    "  context_tokens = EXCLUDED.context_tokens, "
                    "  learned_at = EXCLUDED.learned_at, "
                    "  source = EXCLUDED.source",
                    (model, int(vram_mb), context_tokens, source),
                )


__all__ = ["JobsMixin", "model_footprint_vram_mb", "predicted_rss_mb"]
