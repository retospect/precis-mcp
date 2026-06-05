"""Shared renderer for ``view='log'`` across ref kinds.

Both :class:`precis.handlers.finding.FindingHandler` and
:class:`precis.handlers.paper.PaperHandler` expose a ``view='log'``
that reads ``ref_events`` for the ref and renders the chronology
in a human-scannable format. Same shape across kinds — factored
here so the two handlers don't drift.

The renderer is **chronological oldest → newest** so a glance at
the bottom shows "where we are now" without scrolling up. Each
event line is:

    HH:MM:SS  source       event           summary

with a one-line summary that pulls the most relevant payload fields
for the (source, event) pair (the chase's frontier→next, the
fetcher's url, etc.).
"""

from __future__ import annotations

from typing import Any

from precis.errors import Unsupported
from precis.response import Response


def render_event_log(
    store: Any,
    ref_id: int,
    *,
    source: str | None = None,
    limit: int = 50,
) -> Response:
    """Render the last ``limit`` events for ``ref_id``.

    ``source`` filter narrows to one subsystem (e.g. ``'chase'``);
    omitted, all events for the ref. Returns a :class:`Response`
    suitable for direct return from a handler's ``view='log'`` arm.

    Empty event log → "no events recorded" placeholder so the
    rendered surface stays consistent.

    Raises :class:`Unsupported` when the ``ref_events`` table is
    missing on the database (F1) — produces a clean envelope with a
    migration hint instead of leaking the raw ``psycopg.errors.
    UndefinedTable`` traceback through the [error:Internal] fallback.
    """
    try:
        events = store.events_for(ref_id, source=source, limit=limit)
    except Exception as e:
        # Detect "ref_events table doesn't exist" without importing
        # psycopg here (keeps this module portable for stub stores).
        # psycopg.errors.UndefinedTable's pgcode is '42P01'; falling
        # back to a substring match on the message catches the same
        # condition when wrapped by other drivers.
        msg = str(e)
        pgcode = getattr(e, "sqlstate", None) or getattr(
            getattr(e, "diag", None), "sqlstate", None
        )
        if pgcode == "42P01" or ("ref_events" in msg and "does not exist" in msg):
            raise Unsupported(
                "view='log' requires the ref_events table (migration 0009)",
                next="run `precis migrate` to apply pending migrations",
            ) from e
        raise
    if not events:
        return Response(
            body=(
                f"log: no events recorded for ref_id={ref_id}"
                + (f" (source={source!r})" if source else "")
            )
        )

    # store.events_for returns newest-first; flip so we read top-to-bottom
    # as oldest → newest (matches what an operator wants).
    events = list(reversed(events))

    lines: list[str] = []
    header = f"log: {len(events)} event(s) for ref_id={ref_id}"
    if source:
        header += f" (source={source!r})"
    lines.append(header)
    for ev in events:
        hms = ev.ts.strftime("%H:%M:%S")
        summary = _summarise(ev.source, ev.event, ev.payload, ev.cost_usd)
        line = f"  {hms}  {ev.source:<18}  {ev.event:<14}  {summary}"
        if ev.duration_ms is not None:
            line += f"  ({ev.duration_ms} ms)"
        lines.append(line)
    return Response(body="\n".join(lines))


def _summarise(
    source: str,
    event: str,
    payload: dict[str, Any],
    cost_usd: float | None,
) -> str:
    """Pull the most relevant payload fields for one event into a one-liner.

    Per-subsystem vocabularies are codified here so the rendering
    stays terse. Unknown (source, event) pairs fall back to a
    compact JSON dump of the payload.
    """
    if source == "chase":
        return _summarise_chase(event, payload)
    if source.startswith("fetcher:"):
        return _summarise_fetcher(event, payload, cost_usd)
    # Default: compact payload preview.
    if not payload:
        return ""
    items = ", ".join(f"{k}={v!r}" for k, v in list(payload.items())[:3])
    return items


def _summarise_chase(event: str, payload: dict[str, Any]) -> str:
    front = payload.get("frontier") or {}
    nxt = payload.get("next") or {}
    front_str = (
        f"ref={front.get('ref_id')}~{front.get('resolved_ord', front.get('ord'))}"
    )
    if event == "advanced":
        return f"{front_str} → ref={nxt.get('ref_id')}"
    if event == "terminated":
        return f"{front_str} (primary)"
    if event == "waiting":
        return f"{front_str} (stub has no chunks yet)"
    if event == "dead":
        return f"{front_str} ({payload.get('reason', 'unknown')})"
    if event == "multi":
        n = nxt.get("candidates", "?")
        return f"{front_str} ({n} candidates — needs disambiguation)"
    if event == "cycle":
        return f"{front_str} → ref={nxt.get('ref_id')} would revisit"
    if event == "failed":
        return payload.get("error", "(no detail)")
    return _generic_payload(payload)


def _summarise_fetcher(
    event: str, payload: dict[str, Any], cost_usd: float | None
) -> str:
    if event == "fetch_ok":
        url = payload.get("url", "")
        size = payload.get("size_bytes")
        size_str = f" ({size} bytes)" if size else ""
        return f"OK {url}{size_str}"
    if event == "no_oa_version":
        return "no OA URL available"
    if event == "fetch_failed":
        return payload.get("error", "(no detail)")
    if event == "rate_limited":
        return "Unpaywall rate limit hit"
    return _generic_payload(payload)


def _generic_payload(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    items = ", ".join(f"{k}={v!r}" for k, v in list(payload.items())[:3])
    return items


__all__ = ["render_event_log"]
