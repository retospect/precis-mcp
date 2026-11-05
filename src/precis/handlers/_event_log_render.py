"""Shared renderer for ``view='log'`` across ref kinds.

Both :class:`precis.handlers.finding.FindingHandler` and
:class:`precis.handlers.paper.PaperHandler` expose a ``view='log'``
that reads ``ref_events`` for the ref and renders the chronology
in a human-scannable format. Same shape across kinds — factored
here so the two handlers don't drift.

Paper and finding logs stay chronological oldest → newest; memory
logs are newest first, with bounded windows and an event-id drilldown.
Summaries clip payload values for every kind; stored events are untouched.
Each event line is:

    YYYY-MM-DDTHH:MM:SSZ  source       event           summary

— ISO-8601 UTC with the date, so before/after a deploy is legible
from the log alone (gr517627: a bare ``HH:MM:SS`` couldn't say
which day an event ran). The one-line summary pulls the most
relevant payload fields for the (source, event) pair (the chase's
frontier→next, the fetcher's url, etc.).
"""

from __future__ import annotations

import json
import reprlib
from datetime import UTC
from typing import TYPE_CHECKING, Any

from precis._result_budget import result_context_cap
from precis.errors import BadInput, NotFound, Unsupported
from precis.format import render_agent_table
from precis.response import Response

if TYPE_CHECKING:
    from precis.store.store import Store


def render_event_log(
    store: Store,
    ref_id: int,
    *,
    source: str | None = None,
    limit: int = 50,
    newest_first: bool = False,
    offset: int = 0,
    event_id: int | None = None,
) -> Response:
    """Render the last ``limit`` events for ``ref_id``.

    ``source`` filter narrows to one subsystem (e.g. ``'chase'``);
    omitted, all events for the ref. Returns a :class:`Response`
    suitable for direct return from a handler's ``view='log'`` arm.
    Memory opts into ``newest_first`` for counted windows and full-event
    reads; ``offset`` counts payload characters when ``event_id`` is set.

    Empty event log → "no events recorded" placeholder so the
    rendered surface stays consistent.

    Raises :class:`Unsupported` when the ``ref_events`` table is
    missing on the database (F1) — produces a clean envelope with a
    migration hint instead of leaking the raw ``psycopg.errors.
    UndefinedTable`` traceback through the [error:Internal] fallback.
    """
    try:
        if newest_first:
            return _render_memory_log(
                store, ref_id, limit=limit, offset=offset, event_id=event_id
            )
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
                "view='log' requires the ref_events table (0001_initial.sql)",
                next="run `precis migrate` to apply pending migrations",
            ) from e
        raise
    if not events:
        return Response(
            body=(
                f"log: no events recorded for ref_id={ref_id}"
                + (f" (source={source!r})" if source else "")
            ),
            result_count=0,
        )

    # Other kinds retain their chronological presentation.
    events = list(reversed(events))

    lines: list[str] = []
    header = f"log: {len(events)} event(s) for ref_id={ref_id}"
    if source:
        header += f" (source={source!r})"
    lines.append(header)
    rows = []
    for ev in events:
        ts = ev.ts
        if ts.tzinfo is not None:
            ts = ts.astimezone(UTC)
        rows.append(
            {
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": ev.source,
                "event": ev.event,
                "summary": _summarise(ev.source, ev.event, ev.payload, ev.cost_usd),
                "ms": ev.duration_ms if ev.duration_ms is not None else "",
            }
        )
    lines.append(
        render_agent_table(rows, schema=["ts", "source", "event", "summary", "ms"])
    )
    return Response(body="\n".join(lines), result_count=len(events))


def _log_call(ref_id: int, **args: int) -> str:
    extra = f", args={args!r}" if args else ""
    return f"get(kind='memory', id={ref_id}, view='log'{extra})"


def log_integer(value: Any, name: str, ref_id: int, *, minimum: int) -> int:
    """Reject lossy coercions and supply a working recovery read."""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        parsed = minimum - 1
    if parsed < minimum:
        raise BadInput(
            f"{name}= must be an integer >= {minimum}, got {value!r}",
            next=_log_call(ref_id),
        )
    return parsed


def _render_memory_log(
    store: Store, ref_id: int, *, limit: int, offset: int, event_id: int | None
) -> Response:
    """Fit whole event lines, or an explicitly selected payload, before transport.

    Counts never load bodies. A full event uses character offsets into its
    JSON payload so even a single large old_text can be read to completion.
    """
    budget = max(1, min(12_000, result_context_cap()) - 768)
    if event_id is not None:
        events = store.events_for(ref_id, event_id=event_id, limit=1)
        if not events:
            raise NotFound(
                f"event_id={event_id} not found for memory {ref_id}",
                next=_log_call(ref_id),
            )
        ev = events[0]
        payload = json.dumps(ev.payload, ensure_ascii=False, indent=2, sort_keys=True)
        if offset > len(payload):
            raise BadInput(
                f"offset={offset} exceeds payload length {len(payload)}",
                next=_log_call(ref_id, event_id=event_id),
            )
        full_budget = max(256, min(12_000, result_context_cap()) - 768)
        part = (
            payload[offset:]
            .encode("utf-8")[:full_budget]
            .decode("utf-8", errors="ignore")
        )
        end = offset + len(part)
        header = (
            f"log: memory {ref_id} event_id={event_id} "
            f"payload characters {offset}:{end} of {len(payload)}\n"
        )
        body = header + part
        next_call = _log_call(ref_id, event_id=event_id, offset=end)
        if end < len(payload):
            body += f"\n… payload clipped at character {end}; next: {next_call}"
        return Response(body=body, result_count=1, result_budget_hint=next_call)

    total = store.count_events_for(ref_id)
    events = store.events_for(ref_id, limit=limit, offset=offset)
    lines: list[str] = []
    used = 0
    for ev in events:
        ts = ev.ts.astimezone(UTC) if ev.ts.tzinfo else ev.ts
        line = (
            f"event_id={ev.event_id} {ts:%Y-%m-%dT%H:%M:%SZ} "
            f"{_one_line(ev.source, 64)} {_one_line(ev.event, 64)} "
            f"{_summarise(ev.source, ev.event, ev.payload, ev.cost_usd)}"
        )
        size = len(line.encode("utf-8")) + 1
        if lines and used + size > budget:
            break
        lines.append(line)
        used += size
    end = offset + len(lines)
    header = (
        f"log: {len(lines)} of {total} event(s) for memory {ref_id} "
        f"(newest first, offset={offset})"
    )
    if total == 0:
        header += " — no events recorded"
    body = "\n".join([header, *lines])
    if lines:
        body += f"\nFull event: {_log_call(ref_id, event_id=events[0].event_id)}"
    next_call = _log_call(ref_id, limit=limit, offset=end)
    if end < total:
        body += f"\nnext: {next_call}"
    elif offset and not lines:
        body += f"\nnext: {_log_call(ref_id)}"
    return Response(body=body, result_count=len(lines), result_budget_hint=next_call)


def _one_line(value: str, limit: int = 384) -> str:
    line = " ".join(value.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _preview_value(value: Any, key: str = "", depth: int = 0) -> Any:
    """Bound nested payloads as well as the familiar body-edit keys."""
    if isinstance(value, str):
        if key in {"old_text", "new_text"} or len(value) > 96:
            preview = value[: min(64, max(0, len(value) - 1))]
            return f"<{len(value)} chars: {preview!r}…>"
        return value
    if depth >= 3:
        return reprlib.repr(value)
    if isinstance(value, dict):
        return {
            _one_line(str(k), 64): _preview_value(v, str(k), depth + 1)
            for k, v in list(value.items())[:8]
        }
    if isinstance(value, list):
        return [_preview_value(v, depth=depth + 1) for v in value[:8]]
    return value


def _summarise(
    source: str,
    event: str,
    payload: dict[str, Any],
    cost_usd: float | None,
) -> str:
    """Pull the most relevant payload fields for one event into a one-liner.

    Per-subsystem vocabularies are codified here so the rendering
    stays terse. Unknown (source, event) pairs fall back to a
    bounded preview of the payload.
    """
    preview = {k: _preview_value(v, k) for k, v in payload.items()}
    if source == "chase":
        summary = _summarise_chase(event, preview)
    elif source.startswith("fetcher:"):
        summary = _summarise_fetcher(event, preview, cost_usd)
    else:
        summary = _generic_payload(preview)
    return _one_line(summary)


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
        # gr517168: a stub the stance gate just rejected used to render
        # as "stub has no chunks yet" — the masked rejection is what made
        # the acquiring stall invisible. Name the rejection instead.
        rejected = front.get("rejected")
        if rejected:
            return f"{front_str} (stub rejected: {rejected})"
        if front.get("verify_retry"):
            return f"{front_str} (verify error — will retry)"
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
        return _summarise_rate_limited(payload)
    return _generic_payload(payload)


#: Display names for the ``provider`` key of a ``rate_limited`` payload.
_PROVIDER_NAMES = {
    "unpaywall": "Unpaywall",
    "arxiv": "arXiv",
    "crossref": "Crossref",
    "openalex": "OpenAlex",
    "s2": "Semantic Scholar",
}


def _summarise_rate_limited(payload: dict[str, Any]) -> str:
    """A shared-limiter refusal (``fetch_oa._local_rate_limited`` and the
    arXiv legs) carries ``provider`` and ``limiter='shared'``; the legacy
    Unpaywall HTTP 429 carries neither, so no ``provider`` means Unpaywall."""
    provider = str(payload.get("provider") or "unpaywall")
    name = _PROVIDER_NAMES.get(provider, provider)
    line = f"{name} rate limit hit"
    if payload.get("limiter"):
        line += f" (limiter={payload['limiter']})"
    return line


def _generic_payload(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    items = ", ".join(f"{k}={v!r}" for k, v in list(payload.items())[:3])
    return items


__all__ = ["render_event_log"]
