"""One route state for a pcb design — the single definition every route
consumer reads, replacing per-call-site status checks that had drifted
(a zero-net design read ``False`` to one gate and "not checkable" to the
checklist).

Success is **done**: every net that ought to be routed is realized and the
latest fresh DRC run has no live (unwaived) error. The four states:

- ``NOT_STARTED`` — the design has no nets, or no net has left
  ``'unrouted'`` (no route run yet, or every net ripped since).
- ``PARTIAL`` — some net is not ``'realized'`` (``unrouted``, ``sketched``
  or ``failed``). A dangling (<2-member) net counts as realized once a route
  run has written its noted ``'realized'`` row (``ir.net_member_counts`` is
  the writer's rule); before that run it is pending like any other.
- ``DRC_FAIL`` — every net realized, but the latest DRC run is fresh and
  carries live error findings. Persistent DRC_FAIL is information (board too
  small, too few layers), so the findings ride along.
- ``DONE`` — every net realized and no fresh DRC run says otherwise. A
  missing or stale run leaves ``drc_clean`` ``None``: routing is done, the
  DRC verdict is not in.

Consumers: the ``route_complete`` and ``netlist_drc_clean`` gate evaluators
(each collapses to the ``bool | None`` the generic machine wants — the
shared Evaluator contract is not widened), the ``all-nets-routed`` checklist
tool item, ``view='route-status'``, the board ``## route status`` line, the
derived design phase, and DRC's unrouted-net input. The per-net rows come
from :meth:`Store.pcb_route_status`; the summary buckets from
:func:`summarize`, which :meth:`Store._pcb_board_meta` also uses so the
headline and the detail view cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from precis.pcb import format_route_summary, route_summary_status


class RouteState(StrEnum):
    NOT_STARTED = "not_started"
    PARTIAL = "partial"
    DRC_FAIL = "drc_fail"
    DONE = "done"


@dataclass(frozen=True)
class RouteStatus:
    """A design's route state plus the evidence behind it."""

    state: RouteState
    #: Per-net rows (``name``/``net_class``/``domain``/``status``/``note``).
    nets: list[dict[str, Any]] = field(default_factory=list)
    #: Summary buckets — :func:`summarize` of ``nets``.
    counts: dict[str, int] = field(default_factory=dict)
    #: Nets not yet ``'realized'`` — what keeps the state below DRC_FAIL.
    pending: list[dict[str, Any]] = field(default_factory=list)
    #: Every net realized: ``None`` while NOT_STARTED, else the verdict.
    routed: bool | None = None
    drc_run_id: str | None = None
    drc_stale: bool = False
    #: Live (unwaived) ``severity='error'`` findings of the latest run.
    drc_errors: list[dict[str, Any]] = field(default_factory=list)
    #: ``None`` when no DRC run is recorded or the latest predates the
    #: board's last edit (gr492096); else no live error findings.
    drc_clean: bool | None = None


def summarize(nets: list[dict[str, Any]]) -> dict[str, int]:
    """Summary bucket counts, a dangling net under ``'dangling'``."""
    counts: dict[str, int] = {}
    for r in nets:
        key = route_summary_status(r.get("status"), r.get("note"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def started(counts: dict[str, int]) -> bool:
    """Some net has left ``'unrouted'`` — a route run has written state."""
    return any(k != "unrouted" for k in counts)


def summary_line(n_nets: int, counts: dict[str, int]) -> str:
    """The board's ``## route status`` text; a board-less design has no
    counts, so every net reads unrouted."""
    if counts:
        return format_route_summary(counts)
    return f"{n_nets} net(s): {n_nets} unrouted"


def classify(
    nets: list[dict[str, Any]],
    *,
    drc_run_id: str | None = None,
    drc_findings: list[dict[str, Any]] | None = None,
    drc_stale: bool = False,
) -> RouteStatus:
    """The pure rule: per-net rows and the latest DRC run → a state."""
    counts = summarize(nets)
    errors = [
        f
        for f in drc_findings or []
        if f.get("severity") == "error" and not f.get("waived_by")
    ]
    pending = [r for r in nets if r["status"] != "realized"]
    if not nets or not started(counts):
        state = RouteState.NOT_STARTED
    elif pending:
        state = RouteState.PARTIAL
    elif drc_run_id is not None and not drc_stale and errors:
        state = RouteState.DRC_FAIL
    else:
        state = RouteState.DONE
    return RouteStatus(
        state=state,
        nets=list(nets),
        counts=counts,
        pending=pending,
        routed=(
            None if state is RouteState.NOT_STARTED else state is not RouteState.PARTIAL
        ),
        drc_run_id=drc_run_id,
        drc_stale=drc_stale,
        drc_errors=errors,
        drc_clean=None if drc_run_id is None or drc_stale else not errors,
    )


def route_state(store: Any, ref_id: int, *, drc: bool = True) -> RouteStatus:
    """The design's route state from the store. ``drc=False`` skips the DRC
    lookups for a caller that only reads the per-net side (``routed``,
    ``counts``, ``pending``); its ``state`` then never reads ``DRC_FAIL``."""
    nets = store.pcb_route_status(ref_id)
    if not drc:
        return classify(nets)
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    return classify(
        nets,
        drc_run_id=run_id,
        drc_findings=findings,
        drc_stale=store.pcb_drc_run_stale(ref_id, run_id),
    )
