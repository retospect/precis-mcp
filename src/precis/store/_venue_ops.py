"""``venue_metrics`` — per-ISSN journal venue metrics (gr606422).

Mixin on :class:`precis.store.Store`. Migration ``0293_venue_metrics.sql``
defines the table; :mod:`precis.ingest.venue_metrics` writes it (Scimago
SJR yearly CSV + OpenAlex ``/sources`` snapshots, via ``precis
venue-metrics``) and the paper overview + ``min_quartile`` search filter
read it.

``issn`` is stored normalized — uppercase, hyphens stripped
(``13858947``) — the same normalisation the SQL readers apply to
``refs.meta->>'issn'``, so the two join without a format column. A venue
lists several ISSNs (print + electronic): each lands its own row, all
carrying the same metrics.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_ISSN_STRIP_RE = re.compile(r"[^0-9Xx]")

#: SQL fragment normalising ``refs.meta->>'issn'`` to the stored form —
#: shared by the overview lookup and the ``min_quartile`` scope resolver
#: so the two can never disagree on format.
ISSN_NORMALISE_SQL = "upper(regexp_replace(%s, '[^0-9Xx]', '', 'g'))"


def normalise_issn(raw: str | None) -> str:
    """Normalise an ISSN to the stored form: uppercase alnum, no hyphens.

    ``"1385-8947"`` → ``"13858947"``. Returns ``""`` for anything that
    doesn't reduce to exactly 8 characters — a malformed ISSN must never
    key a metrics row.
    """
    compact = _ISSN_STRIP_RE.sub("", str(raw or "")).upper()
    return compact if len(compact) == 8 else ""


@dataclass(frozen=True)
class VenueMetrics:
    """The merged read a paper overview renders (gr606422).

    Two legs merged per ISSN: the SJR row nearest the paper's year —
    ``sjr``/``sjr_quartile``/``sjr_year``, at-or-before preferred, the
    newest fallback when the venue's earliest vintage postdates the
    paper — and the newest row carrying OpenAlex snapshot columns
    (``oa_year`` is that fetch year, not a vintage). ``None`` fields mean
    the feed never reported them; a venue Scimago doesn't index still
    shows its OpenAlex half.
    """

    issn: str
    title: str | None
    sjr: float | None
    sjr_quartile: str | None
    sjr_year: int | None
    openalex_source_id: str | None
    openalex_2yr_mean_citedness: float | None
    openalex_h_index: int | None
    oa_year: int | None


class VenueMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def upsert_venue_metrics(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Insert/refresh ``venue_metrics`` rows; returns the row count.

        Each row needs ``issn`` (normalized — :func:`normalise_issn` is
        applied defensively, unparseable values are skipped) and ``year``;
        the metric columns are optional and fill independently:
        ``COALESCE(EXCLUDED.x, t.x)`` keeps a Scimago upsert from nulling
        OpenAlex columns on a shared (issn, year) row and vice versa.
        ``updated_at`` restamps on every write.
        """
        params: list[tuple[Any, ...]] = []
        for r in rows:
            issn = normalise_issn(str(r.get("issn") or ""))
            if not issn:
                continue
            try:
                year = int(r["year"])
            except (KeyError, TypeError, ValueError):
                continue
            params.append(
                (
                    issn,
                    year,
                    r.get("title"),
                    r.get("sjr"),
                    r.get("sjr_quartile"),
                    r.get("openalex_source_id"),
                    r.get("openalex_2yr_mean_citedness"),
                    r.get("openalex_h_index"),
                )
            )
        if not params:
            return 0
        with self.pool.connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO venue_metrics (issn, year, title, sjr, "
                "sjr_quartile, openalex_source_id, "
                "openalex_2yr_mean_citedness, openalex_h_index) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (issn, year) DO UPDATE SET "
                "title = COALESCE(EXCLUDED.title, venue_metrics.title), "
                "sjr = COALESCE(EXCLUDED.sjr, venue_metrics.sjr), "
                "sjr_quartile = COALESCE(EXCLUDED.sjr_quartile, "
                "venue_metrics.sjr_quartile), "
                "openalex_source_id = COALESCE(EXCLUDED.openalex_source_id, "
                "venue_metrics.openalex_source_id), "
                "openalex_2yr_mean_citedness = "
                "COALESCE(EXCLUDED.openalex_2yr_mean_citedness, "
                "venue_metrics.openalex_2yr_mean_citedness), "
                "openalex_h_index = COALESCE(EXCLUDED.openalex_h_index, "
                "venue_metrics.openalex_h_index), "
                "updated_at = now()",
                params,
            )
            conn.commit()
        return len(params)

    def venue_metrics_for_issn(
        self, issn: str, *, year: int | None = None
    ) -> VenueMetrics | None:
        """The venue's merged metrics for one normalized-or-raw ISSN.

        ``year`` is the citing paper's publication year: the SJR leg
        prefers the newest vintage at-or-before it (the venue's standing
        when the paper appeared), falling back to the earliest available
        for a paper that predates the feed, and to the latest vintage
        when ``year`` is ``None``. The OpenAlex leg always takes the
        newest snapshot row. Returns ``None`` when the ISSN normalises to
        nothing stored.
        """
        key = normalise_issn(issn)
        if not key:
            return None
        # No paper year → every vintage counts as "at-or-before" so the
        # newest wins on the proximity tiebreak.
        bound = int(year) if year is not None else 32767
        with self.pool.connection() as conn:
            sjr_row = conn.execute(
                "SELECT title, sjr, sjr_quartile, year FROM venue_metrics "
                "WHERE issn = %s AND (sjr IS NOT NULL OR sjr_quartile IS NOT NULL) "
                # At-or-before vintages rank first; the proximity tiebreak
                # then picks the nearest — newest ≤ year, else earliest >.
                "ORDER BY (year <= %s) DESC, abs(year - %s) LIMIT 1",
                (key, bound, bound),
            ).fetchone()
            oa_row = conn.execute(
                "SELECT title, openalex_source_id, "
                "openalex_2yr_mean_citedness, openalex_h_index, year "
                "FROM venue_metrics WHERE issn = %s "
                "AND openalex_source_id IS NOT NULL "
                "ORDER BY year DESC LIMIT 1",
                (key,),
            ).fetchone()
        if sjr_row is None and oa_row is None:
            return None
        title = (sjr_row[0] if sjr_row else None) or (oa_row[0] if oa_row else None)
        return VenueMetrics(
            issn=key,
            title=title,
            sjr=sjr_row[1] if sjr_row else None,
            sjr_quartile=sjr_row[2] if sjr_row else None,
            sjr_year=sjr_row[3] if sjr_row else None,
            openalex_source_id=oa_row[1] if oa_row else None,
            openalex_2yr_mean_citedness=oa_row[2] if oa_row else None,
            openalex_h_index=oa_row[3] if oa_row else None,
            oa_year=oa_row[4] if oa_row else None,
        )
