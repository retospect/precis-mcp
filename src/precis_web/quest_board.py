"""Board panel for a quest served by ``kind='pcb'`` refs (gr488206).

A PCB quest has no meaningful Pareto front: what matters is each board's
state and the next action on it. :func:`board_panel` builds one column per
served board — route completion, DRC/ERC counts, datasheet-review verdicts,
BOM coverage, fab readiness — plus the status of the quest's feature todos.
The quest hub (``routes/refs.py::_quest_detail``) renders it in place of the
frontier; the action buttons post to ``POST /pcb/{slug}/op``
(``routes/pcb.py``), which queues the existing ops through the normal
dispatch write path.

Every section is read-only and isolated: one section's failure (a store
error, a board with no DRC run yet) shows as "unavailable" in that cell and
never 500s the hub.
"""

from __future__ import annotations

import collections
import logging
from typing import Any
from urllib.parse import quote

from precis.checklist.status import VERDICTS, compute_statuses
from precis.pcb import ROUTE_DANGLING, route_summary_status
from precis.pcb.catalog import min_unit_price

log = logging.getLogger(__name__)

#: The ledger the datasheet review and the tapeout gate both write to;
#: it applies to every pcb by default (``data/checklists/pcb-tapeout.yaml``).
TAPEOUT_CHECKLIST = "pcb-tapeout"

#: The netlist ERC rules (``PcbHandler._erc_findings``) — they share the
#: DRC finding list and are told apart only by rule name.
ERC_RULES = frozenset(
    {
        "pin_conflict",
        "no_driver",
        "over_voltage",
        "rail_crossing",
        "no_decoupling",
        "over_current",
        "passive_rating",
    }
)

#: The pcb views the panel links, in display order. Each opens
#: ``GET /pcb/{slug}/view/{name}`` (``routes/pcb.py``); ``tapeout`` is the
#: checklist status for the board rather than a pcb view.
LINKED_VIEWS: tuple[str, ...] = (
    "route-status",
    "congestion",
    "stackup",
    "drc",
    "tapeout",
)

#: Job types whose latest run the panel reports per board.
_JOB_TYPES = ("pcb_route", "pcb_place")


def pcb_servers(live_servers: list[Any]) -> list[Any]:
    """The live ``serves`` refs of kind ``pcb``, in link order."""
    return [s for s in live_servers if getattr(s, "kind", None) == "pcb"]


def declares_objectives(quest_meta: dict[str, Any] | None) -> bool:
    """True when the quest declares its own numeric objectives
    (``meta.rubric_objectives``) — the condition for a Pareto front on a
    board quest. The frontier module falls back to a default energy
    objective otherwise, which means nothing for a board."""
    raw = (quest_meta or {}).get("rubric_objectives")
    return bool(raw)


def _status_tag(tags: Any) -> str | None:
    """The ``STATUS:`` value off a tag list (job or todo)."""
    for t in tags or ():
        s = str(t)
        if s.startswith("STATUS:"):
            return s[len("STATUS:") :]
    return None


def last_job(store: Any, pcb_ref_id: int, job_type: str) -> dict[str, Any] | None:
    """The newest ``job_type`` job for one board: id, status, failure class
    and the error's first line. ``None`` when the board never ran one."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, meta->>'failure_class', meta->>'error', created_at "
            "FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'job_type' = %s "
            "AND meta->'params'->>'pcb_ref_id' = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (job_type, str(pcb_ref_id)),
        ).fetchone()
    if row is None:
        return None
    job_id = int(row[0])
    error = (row[2] or "").strip().splitlines()
    return {
        "id": job_id,
        "status": _status_tag(store.tags_for(job_id)) or "unknown",
        "failure_class": row[1],
        "error": error[0][:200] if error else "",
        "created": str(row[3])[:16] if row[3] is not None else "",
    }


def _route_section(store: Any, ref_id: int, design: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = dict(design.get("route_status") or {})
    reasons: collections.Counter[str] = collections.Counter()
    for net in store.pcb_route_status(ref_id):
        if route_summary_status(net.get("status"), net.get("note")) != "failed":
            continue
        note = str(net.get("note") or "")
        body = note.split(":", 1)[1] if note.startswith("failed:") else ""
        kinds = [k.strip() for k in body.split(",") if k.strip()]
        reasons.update(kinds or ["unspecified"])
    dangling = counts.get(ROUTE_DANGLING, 0)
    total = sum(counts.values())
    return {
        "total": total,
        "routable": total - dangling,
        "routed": counts.get("realized", 0),
        "failed": counts.get("failed", 0),
        "unrouted": counts.get("unrouted", 0),
        "sketched": counts.get("sketched", 0),
        "dangling": dangling,
        "fail_reasons": reasons.most_common(),
    }


def _drc_section(store: Any, ref_id: int) -> dict[str, Any]:
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    drc: collections.Counter[str] = collections.Counter()
    erc: collections.Counter[str] = collections.Counter()
    rules: collections.Counter[str] = collections.Counter()
    for f in findings:
        if f.get("waived_by"):
            continue
        sev = str(f.get("severity") or "")
        rule = str(f.get("rule") or "")
        (erc if rule in ERC_RULES else drc)[sev] += 1
        if sev == "error":
            rules[rule] += 1
    return {
        "has_run": run_id is not None,
        "drc_errors": drc["error"],
        "drc_warns": drc["warn"],
        "erc_errors": erc["error"],
        "erc_warns": erc["warn"],
        "top_rules": rules.most_common(4),
    }


def _checklist_sections(
    store: Any, ref_id: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(review, tapeout) off the ``pcb-tapeout`` ledger. Review counts the
    recorded judgment verdicts by value and origin; tapeout counts the
    blocking items still open, tool checks included."""
    checklist = store.checklist_get(TAPEOUT_CHECKLIST)
    if checklist is None:
        return None, None
    items = store.checklist_items_current(checklist["id"], target_ref_id=ref_id)
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=ref_id, checklist_id=checklist["id"]
    )
    statuses = compute_statuses(
        store, items=items, verdicts=verdicts, kind="pcb", ref_id=ref_id
    )
    by_verdict = {v: 0 for v in VERDICTS}
    origins: collections.Counter[str] = collections.Counter()
    stale = not_checked = 0
    for st in statuses:
        if st.source != "ledger":
            continue
        if st.status in by_verdict:
            by_verdict[st.status] += 1
            origins[str((st.verdict or {}).get("origin") or "human")] += 1
        elif st.status.startswith("stale"):
            stale += 1
        elif st.status == "not checked":
            not_checked += 1
    review = {
        "verdicts": by_verdict,
        "machine": origins["machine"],
        "human": origins["human"],
        "stale": stale,
        "not_checked": not_checked,
        "items": sum(1 for st in statuses if st.source == "ledger"),
    }
    blocking = [st for st in statuses if st.blocking]
    open_blocking = [
        st for st in blocking if st.status not in ("pass", "n/a", "waived")
    ]
    tapeout = {
        "items": len(statuses),
        "blocking": len(blocking),
        "blocking_open": len(open_blocking),
        "failing": [st.name for st in open_blocking if st.status == "fail"][:6],
    }
    return review, tapeout


def _bom_section(store: Any, ref_id: int, design: dict[str, Any]) -> dict[str, Any]:
    instances = design.get("instances") or []
    lines = store.pcb_bom_availability(ref_id)
    mpn_by_lcsc = {
        str(r["part_lcsc"]): r.get("mfr_part")
        for r in lines
        if r.get("part_lcsc") and r.get("mfr_part")
    }
    with_lcsc = with_mpn = 0
    for inst in instances:
        lcsc = inst.get("part_lcsc")
        part_meta = inst.get("part") if isinstance(inst.get("part"), dict) else {}
        if lcsc:
            with_lcsc += 1
        if part_meta.get("mpn") or (lcsc and mpn_by_lcsc.get(str(lcsc))):
            with_mpn += 1
    stock_known = stock_short = priced = 0
    cost = 0.0
    for r in lines:
        if not r.get("part_lcsc"):
            continue
        qty = int(r.get("qty") or 0)
        stock = r.get("stock")
        if stock is not None:
            stock_known += 1
            if int(stock) < qty or r.get("discontinued"):
                stock_short += 1
        unit = min_unit_price(r.get("price"))
        if unit is not None:
            priced += 1
            cost += unit * qty
    return {
        "parts": len(instances),
        "with_lcsc": with_lcsc,
        "with_mpn": with_mpn,
        "lines": len(lines),
        "lcsc_lines": sum(1 for r in lines if r.get("part_lcsc")),
        "stock_known": stock_known,
        "stock_short": stock_short,
        "priced": priced,
        "unit_cost": round(cost, 2) if priced else None,
    }


def _order_section(design: dict[str, Any]) -> dict[str, Any] | None:
    board_meta = (design.get("board") or {}).get("meta") or {}
    order = board_meta.get("jlc_order")
    if not isinstance(order, dict):
        return None
    ident = (
        order.get("order_id")
        or order.get("batch_num")
        or order.get("order_uuid")
        or order.get("file_key")
    )
    return {
        "state": order.get("state") or "unknown",
        "qty": order.get("qty"),
        "ident": str(ident) if ident else "",
        "added_at": str(order.get("added_at") or "")[:16],
    }


def _section(errors: list[str], name: str, fn: Any, *a: Any) -> Any:
    try:
        return fn(*a)
    except Exception:
        log.warning("quest board: %s section failed", name, exc_info=True)
        errors.append(name)
        return None


def board_column(store: Any, ref: Any) -> dict[str, Any]:
    """One served board's column. A failed section is ``None`` and its
    name lands in ``errors``; the template shows it as unavailable."""
    slug = str(getattr(ref, "slug", None) or ref.id)
    errors: list[str] = []
    design = _section(errors, "design", store.pcb_load, ref.id) or {}
    route = _section(errors, "route", _route_section, store, ref.id, design)
    jobs = {jt: _section(errors, jt, last_job, store, ref.id, jt) for jt in _JOB_TYPES}
    drc = _section(errors, "drc", _drc_section, store, ref.id)
    checks = _section(errors, "checklist", _checklist_sections, store, ref.id)
    review, tapeout = checks if checks is not None else (None, None)
    bom = _section(errors, "bom", _bom_section, store, ref.id, design)
    board_meta = (design.get("board") or {}).get("meta") or {}
    title = (getattr(ref, "title", None) or slug).split("\n", 1)[0]
    return {
        "slug": slug,
        "ref_id": ref.id,
        "title": title,
        "page_url": f"/pcb/{quote(slug)}",
        "views": [
            {"name": v, "url": f"/pcb/{quote(slug)}/view/{v}"} for v in LINKED_VIEWS
        ],
        "op_url": f"/pcb/{quote(slug)}/op",
        "route": route,
        "route_job": jobs["pcb_route"],
        "place_job": jobs["pcb_place"],
        "drc": drc,
        "review": review,
        "tapeout": tapeout,
        "phase": board_meta.get("phase"),
        "order": _order_section(design),
        "bom": bom,
        "errors": errors,
    }


def feature_todos(store: Any, live_servers: list[Any]) -> dict[str, Any]:
    """The quest's serving todos: per-status counts plus one row each
    (built features are ``done``, wanted ones ``open``)."""
    rows = []
    for s in live_servers:
        if getattr(s, "kind", None) != "todo":
            continue
        status = _status_tag(store.tags_for(s.id)) or "open"
        title = (getattr(s, "title", None) or "").split("\n", 1)[0]
        rows.append({"id": s.id, "title": title[:120], "status": status})
    counts = collections.Counter(r["status"] for r in rows)
    return {"rows": rows, "counts": sorted(counts.items())}


def board_panel(store: Any, live_servers: list[Any]) -> dict[str, Any] | None:
    """The whole panel, or ``None`` when no pcb serves the quest."""
    boards = pcb_servers(live_servers)
    if not boards:
        return None
    return {
        "boards": [board_column(store, b) for b in boards],
        "todos": feature_todos(store, live_servers),
    }


__all__ = [
    "ERC_RULES",
    "LINKED_VIEWS",
    "TAPEOUT_CHECKLIST",
    "board_column",
    "board_panel",
    "declares_objectives",
    "feature_todos",
    "last_job",
    "pcb_servers",
]
