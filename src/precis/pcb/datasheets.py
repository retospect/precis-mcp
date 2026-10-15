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

#: The extraction job chained after a pull (datasheet-facts-mesh): one
#: BIG-tier read per part, enqueued at put time next to the pull —
#: ``precis.workers.job_types.datasheet_facts`` is the worker half.
FACTS_JOB_TYPE = "datasheet_facts"


def idem_key(lcsc: str) -> str:
    return f"{JOB_TYPE}:{lcsc}"


def facts_idem_key(lcsc: str) -> str:
    """One extraction per part; idem dedupe only collapses a job still
    in flight, so a re-put after a terminal run mints a fresh one (a new
    datasheet revision re-extracts)."""
    return f"{FACTS_JOB_TYPE}:{lcsc.strip().upper()}"


#: Terminal STATUS values for a job ref — mirrors ``handlers/job.py``'s
#: tuple without importing the handler layer (this module is read by
#: workers; the three values are the contract's stable end states).
_JOB_TERMINAL = ("succeeded", "failed", "cancelled")


def pull_in_flight(store: Store, lcsc: str) -> bool:
    """A non-terminal ``datasheet_pull`` job exists for this C-number
    (queued, running, or parked) — the facts job parks on it rather than
    recording ``no_datasheet`` halfway through the pull."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs r "
            "WHERE r.kind = 'job' AND r.retired_at IS NULL "
            "  AND r.meta->>'job_type' = %s "
            "  AND r.meta->'params'->>'lcsc' = %s "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' "
            "      AND t.value = ANY(%s)) "
            "LIMIT 1",
            (JOB_TYPE, lcsc.strip().upper(), list(_JOB_TERMINAL)),
        ).fetchone()
    return row is not None


def _facts_extracted(store: Store, part_ref_id: int | None) -> bool:
    if part_ref_id is None:
        return False
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'datasheet_facts'->>'extracted_at' FROM refs "
            "WHERE ref_id = %s",
            (part_ref_id,),
        ).fetchone()
    return bool(row and row[0])


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


def link_known(store: Store, pcb_ref_id: int) -> int:
    """Add a ``datasheet-of`` link for every C-number on the board whose
    part has none but for which the store already HOLDS the datasheet —
    nothing is fetched or minted. Two ways a ref counts as "known": its
    ``meta.part_lcsc`` names this C-number (the pull job stamps it on
    ingest), or its ``meta.source_url`` equals the part's recorded
    ``parts.datasheet_url`` — the family case (one PDF serving a whole
    series): one datasheet ref, one link per C-number, no duplicates.
    Returns how many parts were newly linked."""
    lcscs = live_lcscs(store, pcb_ref_id)
    if not lcscs:
        return 0
    with store.pool.connection() as conn:
        parts = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT ri.id_value, p.ref_id FROM ref_identifiers ri "
                "JOIN refs p ON p.ref_id = ri.ref_id "
                "WHERE ri.id_kind = 'lcsc' AND ri.id_value = ANY(%s) "
                "  AND p.kind = 'part' AND p.retired_at IS NULL",
                (lcscs,),
            ).fetchall()
        }
        if not parts:
            return 0
        linked_parts = {
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT l.dst_ref_id FROM links l "
                "JOIN refs s ON s.ref_id = l.src_ref_id AND s.retired_at IS NULL "
                "WHERE l.relation = 'datasheet-of' AND l.dst_ref_id = ANY(%s)",
                (list(parts.values()),),
            ).fetchall()
        }
        candidates = conn.execute(
            "SELECT ri.id_value, d.ref_id FROM ref_identifiers ri "
            "JOIN refs d ON d.kind = 'datasheet' AND d.retired_at IS NULL "
            "  AND (d.meta->>'part_lcsc' = ri.id_value "
            "       OR (d.meta->>'source_url' IS NOT NULL "
            "           AND d.meta->>'source_url' <> '' "
            "           AND d.meta->>'source_url' = ("
            "             SELECT datasheet_url FROM parts "
            "             WHERE parts.lcsc = ri.id_value))) "
            "WHERE ri.id_kind = 'lcsc' AND ri.id_value = ANY(%s) "
            "ORDER BY ri.id_value, d.ref_id",
            (lcscs,),
        ).fetchall()
    by_lcsc: dict[str, int] = {}
    for lcsc, ds_id in candidates:
        by_lcsc.setdefault(str(lcsc), int(ds_id))
    linked = 0
    for lcsc in lcscs:
        part_id = parts.get(lcsc)
        ds_id = by_lcsc.get(lcsc)
        if part_id is None or ds_id is None or part_id in linked_parts:
            continue
        store.add_link(
            src_ref_id=ds_id,
            dst_ref_id=part_id,
            relation="datasheet-of",
            set_by="system",
        )
        linked += 1
    return linked


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


def enqueue_facts(store: Store, job: Any, pcb_ref_id: int) -> int:
    """Enqueue one ``datasheet_facts`` job per live C-number that has (or
    is about to have — its pull is queued/in-flight) a datasheet and no
    extracted fact block yet. Called next to :func:`enqueue_pulls` from
    the put path, which owns the handler; the facts job parks itself when
    it wins the race against its pull (:func:`pull_in_flight`). Never
    raises — a queue failure must not undo the put that triggered it."""
    queued = 0
    try:
        lcscs = live_lcscs(store, pcb_ref_id)
        if not lcscs:
            return 0
        states = datasheet_states(store, lcscs)
        for lcsc in lcscs:
            state = states[lcsc]
            has_pull = pull_in_flight(store, lcsc)
            if state["datasheet"] is None and not has_pull:
                continue
            if _facts_extracted(store, state["part_ref_id"]):
                continue
            resp = job.put(
                job_type=FACTS_JOB_TYPE,
                executor="claude_inproc",
                parent_id=pcb_ref_id,
                params={"lcsc": lcsc},
                idem_key=facts_idem_key(lcsc),
            )
            if not getattr(resp, "reused", False):
                queued += 1
    except Exception:
        log.warning(
            "datasheet_facts: enqueue for pcb %s failed", pcb_ref_id, exc_info=True
        )
    return queued


__all__ = [
    "FACTS_JOB_TYPE",
    "JOB_TYPE",
    "RETRY_AFTER",
    "datasheet_state",
    "datasheet_states",
    "enqueue_facts",
    "enqueue_pulls",
    "facts_idem_key",
    "idem_key",
    "is_transient",
    "link_known",
    "live_lcscs",
    "pending_lcscs",
    "pull_in_flight",
    "status_line",
]
