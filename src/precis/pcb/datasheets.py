"""Datasheet auto-pull: the trigger and the read side.

A board's placed parts name themselves by LCSC C-number. When a put leaves a
C-number on a board whose part has neither a ``datasheet-of`` datasheet nor a
recorded pull attempt, :func:`enqueue_pulls` mints one ``datasheet_pull`` job
for it (``precis.workers.job_types.datasheet_pull`` does the fetch, never this
module, never inline in the put).

The outcome of an attempt lives on the part ref's ``meta.datasheet_pull``
(``{status: ok|failed, reason?, detail?, url?, sha256?, at}``), so "no
datasheet" reads differently from "never tried" (:func:`status_line`).
A failed pull is not retried by a later put; ``put(kind='pcb',
args={'op': 'datasheets', 'force': True})`` re-queues it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

JOB_TYPE = "datasheet_pull"


def idem_key(lcsc: str) -> str:
    return f"{JOB_TYPE}:{lcsc}"


def live_lcscs(store: Store, pcb_ref_id: int) -> list[str]:
    """Distinct upper-cased C-numbers of the design's live instances."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT upper(btrim(c.part_lcsc)) "
            "FROM pcb_instances i JOIN pcb_components c "
            "  ON c.component_id = i.component_id "
            "WHERE i.ref_id = %s AND i.retired_at IS NULL "
            "  AND btrim(coalesce(c.part_lcsc, '')) <> '' "
            "ORDER BY 1",
            (pcb_ref_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


#: A failed pull whose reason is transient is retried by a later put once its
#: record is this old; every other failure needs ``force``.
RETRY_AFTER = timedelta(hours=24)


def is_transient(reason: str | None) -> bool:
    """Whether a failure reason may clear by itself: a JLC API error, a
    5xx/429 from the datasheet host, a network error or a timeout. The one
    place the classification lives (permanent: ``no_url``, ``not_pdf``,
    ``too_large``, ``fetch_refused``, other ``http_4xx``, ``ingest_*``)."""
    r = reason or ""
    if r.startswith(("jlc_api_error:", "fetch_error:")) or r == "fetch_timeout":
        return True
    if r.startswith("http_"):
        code = r[5:]
        return code == "429" or (code.isdigit() and 500 <= int(code) <= 599)
    return False


def datasheet_states(store: Store, lcscs: list[str]) -> dict[str, dict[str, Any]]:
    """``{lcsc: {part_ref_id, datasheet: (ref_id, slug) | None, pull: dict |
    None}}`` for every C-number, in ONE query. Never mints."""
    out: dict[str, dict[str, Any]] = {
        c: {"part_ref_id": None, "datasheet": None, "pull": None} for c in lcscs
    }
    if not lcscs:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ri.id_value, p.ref_id, p.meta->'datasheet_pull', "
            "       d.ref_id, ck.id_value "
            "FROM ref_identifiers ri "
            "JOIN refs p ON p.ref_id = ri.ref_id AND p.kind = 'part' "
            "  AND p.retired_at IS NULL "
            "LEFT JOIN LATERAL ( "
            "  SELECT s.ref_id FROM links l "
            "  JOIN refs s ON s.ref_id = l.src_ref_id "
            "  WHERE l.dst_ref_id = p.ref_id AND l.relation = 'datasheet-of' "
            "    AND s.retired_at IS NULL "
            "  ORDER BY s.ref_id LIMIT 1) d ON true "
            "LEFT JOIN ref_identifiers ck ON ck.ref_id = d.ref_id "
            "  AND ck.id_kind = 'cite_key' "
            "WHERE ri.id_kind = 'lcsc' AND ri.id_value = ANY(%s)",
            (list(lcscs),),
        ).fetchall()
    for lcsc, part_id, pull, ds_id, slug in rows:
        out[str(lcsc)] = {
            "part_ref_id": int(part_id),
            "datasheet": (int(ds_id), slug) if ds_id is not None else None,
            "pull": pull if isinstance(pull, dict) else None,
        }
    return out


def datasheet_state(store: Store, lcsc: str) -> dict[str, Any]:
    """One C-number's state (see :func:`datasheet_states`)."""
    key = lcsc.strip().upper()
    return datasheet_states(store, [key])[key]


def status_line(state: dict[str, Any]) -> str:
    """One word-ish cell: the datasheet slug, the failure reason, or
    ``queued`` (no attempt recorded yet)."""
    ds = state["datasheet"]
    pull = state["pull"]
    failed = pull is not None and pull.get("status") == "failed"
    if ds is not None:
        line = f"datasheet {ds[1] or ds[0]}"
        return line + (f" (pull: {pull.get('reason')})" if failed else "")
    if pull is None:
        return "queued"
    if failed:
        return f"failed: {pull.get('reason')}"
    return "ok (datasheet since removed)"


def _aged(pull: dict[str, Any]) -> bool:
    try:
        at = datetime.strptime(str(pull.get("at")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True
    return datetime.now(UTC) - at.replace(tzinfo=UTC) >= RETRY_AFTER


def _needs_pull(state: dict[str, Any], force: bool) -> bool:
    pull = state["pull"]
    failed = pull is not None and pull.get("status") == "failed"
    if state["datasheet"] is not None:
        return force and failed
    if pull is None:
        return True
    if not failed:
        return False
    return force or (is_transient(pull.get("reason")) and _aged(pull))


def pending_lcscs(store: Store, pcb_ref_id: int, *, force: bool = False) -> list[str]:
    """The design's C-numbers that need a pull: no linked datasheet and no
    recorded attempt. A failed attempt counts again when ``force`` is set, or
    when its reason is transient (:func:`is_transient`) and 24 h old."""
    lcscs = live_lcscs(store, pcb_ref_id)
    states = datasheet_states(store, lcscs)
    return [c for c in lcscs if _needs_pull(states[c], force)]


def enqueue_pulls(
    store: Store, job: Any, pcb_ref_id: int, *, force: bool = False
) -> int:
    """Enqueue one ``datasheet_pull`` per pending C-number (idempotency key
    ``datasheet_pull:<LCSC>``, so concurrent puts and several boards sharing a
    part collapse onto one job). ``job`` is the ``job`` kind handler. Returns
    how many jobs were newly queued. Never raises: a failure here must not
    undo the put that triggered it."""
    queued = 0
    try:
        for lcsc in pending_lcscs(store, pcb_ref_id, force=force):
            resp = job.put(
                job_type=JOB_TYPE,
                executor="job_inproc",
                parent_id=pcb_ref_id,
                params={"lcsc": lcsc},
                idem_key=idem_key(lcsc),
            )
            if not getattr(resp, "reused", False):
                queued += 1
    except Exception:
        log.warning(
            "datasheet_pull: enqueue for pcb %s failed", pcb_ref_id, exc_info=True
        )
    return queued


__all__ = [
    "JOB_TYPE",
    "RETRY_AFTER",
    "datasheet_state",
    "datasheet_states",
    "enqueue_pulls",
    "idem_key",
    "is_transient",
    "live_lcscs",
    "pending_lcscs",
    "status_line",
]
