"""reground_worklist — the mined-unheld-DOI acquisition worklist
(gr693791 residual 4).

Stage 5's external leg records the reference-list DOIs and probe hits the
corpus does **not** hold in ``meta.reground_external['unheld_dois']`` and
deliberately never acquires them there — auto-``put`` is unbounded and
cost-bearing, and the paywalled case (fi189542's Krishnan 1997) is a
human call. This module is the human's view over those records: every
mined-unheld DOI, which hubs' regrounds surfaced it, when, and **how many
live draft chunks cite the mining hub(s)** — the demand signal for
approving imports in batches (a DOI mined behind a hub no draft cites is
not worth a fetch; one behind a hub ten paragraphs cite is).

Read-only by construction: nothing here writes, and nothing here puts.
Approval stays a separate, explicit act.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

#: Default row cap — the worklist is for a human's batch decision, not a
# corpus dump.
DEFAULT_LIMIT = 200

_META_REGROUND_EXTERNAL = "reground_external"


@dataclass(frozen=True)
class UnheldDoiRow:
    """One mined-unheld DOI and the demand behind it."""

    doi: str
    #: Every live hub whose reground external report mined this DOI.
    hubs: tuple[int, ...]
    #: The discovery legs that surfaced it (``reference-list``, a probe
    #: source), deduped.
    vias: tuple[str, ...]
    #: Newest report timestamp across the mining hubs (``None`` when the
    #: reports carry no ``at``).
    mined_at: str | None
    #: Live draft chunks citing the mining hub(s) — the citer census, the
    #: stakes of the retire decision this DOI would feed.
    draft_citers: int

    def format(self) -> str:
        hubs = ",".join(f"fi{h}" for h in self.hubs)
        return (
            f"{self.doi}  citers={self.draft_citers}  hubs={hubs}  "
            f"via={','.join(self.vias)}"
            + (f"  mined_at={self.mined_at}" if self.mined_at else "")
        )


@dataclass(frozen=True)
class UnheldDoiWorklist:
    """The whole view: rows plus the hubs whose reports were scanned."""

    rows: tuple[UnheldDoiRow, ...] = ()
    scanned_hubs: int = 0

    def format(self) -> str:
        lines = [r.format() for r in self.rows]
        lines.append(
            f"--- {len(self.rows)} unheld DOI(s) across "
            f"{self.scanned_hubs} reground-external report(s) ---"
        )
        return "\n".join(lines)


def _draft_citer_counts(conn: Any, hub_ref_ids: list[int]) -> dict[int, int]:
    """Live-draft citer count per hub — the retire stage's census
    (:func:`precis.workers.hub_refine._inbound_draft_citers`), kept
    locally per the cross-task seam precedent. One query per hub: the
    view runs over the handful of hubs with external reports, and the
    LIKE shape is the census's own."""
    out: dict[int, int] = {}
    for h in hub_ref_ids:
        row = conn.execute(
            "SELECT count(*) FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE r.kind = 'draft' AND r.retired_at IS NULL AND c.ord >= 0 "
            "AND c.retired_at IS NULL AND c.text LIKE %s",
            (f"%[fi{h}]%",),
        ).fetchone()
        out[h] = int(row[0]) if row else 0
    return out


def unheld_doi_worklist(
    store: Store, *, limit: int = DEFAULT_LIMIT
) -> UnheldDoiWorklist:
    """Every mined-unheld DOI across the live hubs' reground-external
    reports, merged per DOI, with the citer demand — read-only, sorted by
    demand (citers desc, then doi) so the top of the list is the next
    batch worth approving."""
    merged: dict[str, dict[str, Any]] = {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->%s FROM refs "
            "WHERE retired_at IS NULL AND meta->%s IS NOT NULL "
            "ORDER BY ref_id",
            (_META_REGROUND_EXTERNAL, _META_REGROUND_EXTERNAL),
        ).fetchall()
        scanned = 0
        hub_ids: list[int] = []
        reports: list[tuple[int, dict[str, Any]]] = []
        for ref_id, report in rows:
            scanned += 1
            if not isinstance(report, dict):
                continue
            unheld = report.get("unheld_dois")
            if not isinstance(unheld, list) or not unheld:
                continue
            hub_ids.append(int(ref_id))
            reports.append((int(ref_id), report))
        citers = _draft_citer_counts(conn, hub_ids)

    for hub, report in reports:
        at = report.get("at") if isinstance(report.get("at"), str) else None
        for item in report.get("unheld_dois") or []:
            if not isinstance(item, dict):
                continue
            doi = str(item.get("doi") or "").strip().lower()
            if not doi:
                continue
            via = str(item.get("via") or "unknown")
            slot = merged.setdefault(
                doi, {"hubs": [], "vias": [], "at": None, "citers": 0}
            )
            if hub not in slot["hubs"]:
                slot["hubs"].append(hub)
                slot["citers"] += citers.get(hub, 0)
            if via not in slot["vias"]:
                slot["vias"].append(via)
            if at and (slot["at"] is None or at > slot["at"]):
                slot["at"] = at

    out = [
        UnheldDoiRow(
            doi=doi,
            hubs=tuple(sorted(v["hubs"])),
            vias=tuple(v["vias"]),
            mined_at=v["at"],
            draft_citers=v["citers"],
        )
        for doi, v in merged.items()
    ]
    out.sort(key=lambda r: (-r.draft_citers, r.doi))
    return UnheldDoiWorklist(rows=tuple(out[:limit]), scanned_hubs=scanned)


__all__ = ["UnheldDoiRow", "UnheldDoiWorklist", "unheld_doi_worklist"]
