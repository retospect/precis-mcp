"""AgentLogHandler — read / triage side of ``kind='agentlog'``.

Numeric-id ref kind for run-attribution records: one per agentic pass
(plan_tick, operator change, chat follow-up) that touched the corpus,
carrying the assembled prompt + model/source + ``touched`` links to the
chunks it wrote. Subclasses :class:`NumericRefHandler` for the shared
CRUD shape.

Agentlogs are *produced* by run machinery through :mod:`precis.agentlog`
(``open_log`` / ``touch_from_env`` / ``finalize_log``), not by agents —
so this handler omits ``put`` / ``edit``. What it offers the agent
surface is the read / triage half:

    - get(kind='agentlog', id=N)         — read one run + its tags
    - get(kind='agentlog', id='/recent') — recent runs (newest-first)
    - get(kind='agentlog', id='/calls')  — tool_calls mining view
    - get(kind='agentlog', id='/sessions') — per-session ledger roll-up
    - search(kind='agentlog', q=...)     — lexical search over titles
    - tag(id=N, add/remove=[...])        — classify / annotate
    - link(id=N, target='kind:id')       — relate a run to a ref
    - delete(id=N)                       — soft-delete (history pruning)

Like alerts, agentlogs are NOT embedded (``emits_card`` stays False):
surfaced by the ``/agentlogs`` web tab, direct queries, and chunk
``connections`` (the ``touched`` edge), never by semantic search. See
:mod:`precis.agentlog` for the lifecycle.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response

#: Meta keys a bare ``get`` surfaces first, in this order (the triage
#: fields a failed run is read for). Every other small key renders
#: generically after them — a new meta field no longer needs a manual
#: opt-in here to show on a bare get (gr514912).
_TRIAGE_KEYS: tuple[str, ...] = (
    "status",
    "model",
    "model_actual",
    "models_used",
    "error",
)
#: Meta keys never inlined — too bulky for the summary; ``view='raw'``
#: carries them verbatim.
_BULKY_KEYS: frozenset[str] = frozenset({"prompt", "checkpoint", "result"})
#: Caps on the generic section: lines shown and chars per value.
_EXTRA_META_CAP = 8
_META_VALUE_CHARS = 200

#: ``id='/calls?<k>=<v>'`` accepted filter keys — an unknown key refuses
#: with the accepted list (the job ``/logs`` contract, gr520238 ask 3).
_CALLS_FILTER_KEYS = frozenset(
    {
        "kind",
        "verb",
        "view",
        "source",
        "agentlog",
        "errors",
        "since",
        "limit",
        "page_size",
    }
)
#: ``id='/sessions?<k>=<v>'`` accepted filter keys — the per-session
#: roll-up shares the ``/calls`` posture (all optional, all bound).
_SESSIONS_FILTER_KEYS = frozenset(
    {
        "verb",
        "session",
        "since",
        "limit",
        "page_size",
    }
)
#: Group rows a ``/calls`` summary shows before folding to the line count.
_CALLS_GROUP_CAP = 12


def _meta_value(val: Any) -> str:
    """One-line rendering of a meta value, hard-capped."""
    s = val if isinstance(val, str) else json.dumps(val, default=str, sort_keys=True)
    s = " ".join(str(s).split())
    return s if len(s) <= _META_VALUE_CHARS else s[:_META_VALUE_CHARS].rstrip() + "…"


def _fmt_span(seconds: int) -> str:
    """A session wall span as ``45m`` / ``2h5m`` / ``1d3h`` — one
    coarse unit pair, never a float."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600}h"


class AgentLogHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="agentlog",
        title="Agent log",
        description=(
            "Run-attribution record — one per agentic run (plan_tick, "
            "operator change request, chat follow-up) that touched the "
            "corpus. Carries the full assembled prompt, model + source, "
            "and `touched` links to every chunk the run wrote/moved, so a "
            "suspicious chunk walks back to the run that produced it. "
            "Numeric id; GC'd past a retention window (links drop, chunks "
            "stay). Produced by run machinery, not hand-authored. Not "
            "embedded."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # No put/edit: agentlogs are opened by run machinery via
        # precis.agentlog.open_log, never hand-authored through the
        # agent surface.
        supports_put=False,
        supports_edit=False,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "agentlog"
    sense: ClassVar[str] = "agentlog"

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "calls", "sessions")

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        # ``view='calls'``'s row cap, declared as a real param so the
        # args= extras gate passes it (the job /logs precedent, gr508686).
        # Any value on a non-``calls`` read is refused below.
        limit: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        single = (
            id is not None
            and not isinstance(id, list)
            and not (isinstance(id, str) and id.startswith("/"))
        )
        if view == "calls" and single and q is None:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return self._render_calls_view(ref, limit=limit)
        if isinstance(id, str) and (
            id == "/calls"
            or id.startswith("/calls?")
            or id == "/sessions"
            or id.startswith("/sessions?")
        ):
            # A top-level limit= folds into the /calls or /sessions query
            # string (the job /logs fold, gr500645); the id's own value wins.
            if limit is not None:
                path, _, qs = id.partition("?")
                merged = {
                    "limit": str(limit),
                    **dict(parse_qsl(qs, keep_blank_values=True)),
                }
                id = f"{path}?{urlencode(merged)}"
                limit = None
        elif limit is not None:
            raise BadInput(
                "limit= only applies to "
                "get(kind='agentlog', id=<N>, view='calls'), "
                "id='/calls?…' or id='/sessions?…'",
                next="get(kind='agentlog', id=<N>, view='calls', limit=50)",
            )
        return super().get(id=id, view=view, q=q, **_kw)

    def _list_view(self, view: str) -> Response | None:
        # '/calls' and '/calls?kind=paper&view=toc&errors=1' both route
        # here — the query string carries the tool_calls filters.
        if view == "calls" or view.startswith("calls?"):
            _, _, query_string = view.partition("?")
            return self._render_calls_mining(query_string)
        if view == "sessions" or view.startswith("sessions?"):
            _, _, query_string = view.partition("?")
            return self._render_sessions_mining(query_string)
        return super()._list_view(view)

    def _render_calls_view(self, ref: Any, *, limit: Any = None) -> Response:
        """``get(kind='agentlog', id=N, view='calls')`` — the run's own
        ``tool_calls`` rows, oldest-first (gr520238 ask 3: the ledger
        answer to "which views did this run actually call", which used to
        need a transcript-mining pass)."""
        from precis import tool_ledger
        from precis.handlers.job import _parse_logs_int

        n = _parse_logs_int(
            None if limit is None else str(limit),
            default=tool_ledger.CALLS_DEFAULT_LIMIT,
            field="limit",
            example="limit=100 (default), cap 200",
        )
        if n <= 0:
            raise BadInput(
                "limit must be positive", next="limit=100 (default), cap 200"
            )
        n = min(n, tool_ledger.CALLS_MAX_LIMIT)
        total, rows = tool_ledger.calls_for_agentlog(self.store, int(ref.id), limit=n)
        head = f"# agentlog {ref.id} — tool calls"
        if not rows:
            body = (
                f"{head}\n"
                f"no tool_calls rows cite agentlog {ref.id} — the ledger "
                "(migration 0133) only sees calls stamped with this run's "
                "agentlog id; interactive/direct calls carry none."
            )
            return Response(body=body, result_count=0)
        lines = [head, ""]
        lines += [tool_ledger.call_line(r) for r in rows]
        lines.append("")
        lines.append(
            f"newest {len(rows)} of {total} calls (oldest first)"
            if total > len(rows)
            else f"{len(rows)} call(s)"
        )
        return Response(body="\n".join(lines), result_count=len(rows))

    def _render_calls_mining(self, query_string: str) -> Response:
        """``id='/calls?kind=<k>&view=<v>&verb=<v>&source=<s>&agentlog=<N>
        &errors=1&since=<hrs>&limit=<n>'`` — the cross-run mining view:
        a ``GROUP BY (verb, kind, arg_sample view)`` summary plus the
        newest matching ``tool_calls`` rows. All params optional, all
        bound (the ``/logs`` posture)."""
        from precis import tool_ledger
        from precis.handlers._interpret import interpreted
        from precis.handlers.job import _parse_logs_int
        from precis.utils.next_block import render_next_section

        params = dict(parse_qsl(query_string, keep_blank_values=True))
        unknown = sorted(k for k in params if k not in _CALLS_FILTER_KEYS)
        if unknown:
            raise BadInput(
                f"unknown /calls filter(s): {', '.join(unknown)}",
                options=sorted(_CALLS_FILTER_KEYS),
                next="get(kind='agentlog', id='/calls?kind=paper"
                "&view=toc&errors=1&since=168&limit=25')",
            )

        since_hours = _parse_logs_int(
            params.get("since"),
            default=tool_ledger.CALLS_DEFAULT_SINCE_HOURS,
            field="since",
            example="since=168 (default) — hours to look back",
            units=True,
        )
        if since_hours <= 0:
            raise BadInput(
                f"since={since_hours} must be a positive number of hours",
                next="since=168 (default) — hours to look back",
            )
        since_hours = min(since_hours, tool_ledger.CALLS_MAX_SINCE_HOURS)

        # Canonical slice name is ``page_size`` (gr514469); ``limit``
        # stays accepted and announces itself on the interpreted: line.
        readings: list[str] = []
        raw_ps, raw_lim = params.get("page_size"), params.get("limit")
        if raw_ps not in (None, "") and raw_lim not in (None, "") and raw_ps != raw_lim:
            raise BadInput(
                f"conflicting page_size={raw_ps!r} and limit={raw_lim!r}",
                next="pass one — page_size= is the canonical name",
            )
        if raw_lim not in (None, "") and raw_ps in (None, ""):
            readings.append("'limit' read as 'page_size'")
        limit = _parse_logs_int(
            raw_ps if raw_ps not in (None, "") else raw_lim,
            default=tool_ledger.CALLS_DEFAULT_LIMIT,
            field="page_size",
            example="page_size=100 (default), cap 200",
        )
        if limit <= 0:
            raise BadInput(
                f"page_size={limit} must be positive",
                next="page_size=100 (default), cap 200",
            )
        limit = min(limit, tool_ledger.CALLS_MAX_LIMIT)

        errors_raw = (params.get("errors") or "").strip().lower()
        if errors_raw in ("", "0", "false", "no", "off"):
            errors_only = False
        elif errors_raw in ("1", "true", "yes", "on"):
            errors_only = True
        else:
            raise BadInput(
                f"errors={params.get('errors')!r} — pass 1 or 0",
                next="errors=1 — error calls only",
            )

        agentlog_raw = (params.get("agentlog") or "").strip() or None
        agentlog_id: int | None = None
        if agentlog_raw is not None:
            try:
                agentlog_id = int(agentlog_raw)
            except ValueError as exc:
                raise BadInput(
                    f"agentlog={agentlog_raw!r} is not an agentlog ref id",
                    next="agentlog=<N> — an agentlog ref id",
                ) from exc

        out = tool_ledger.calls_mining(
            self.store,
            verb=(params.get("verb") or "").strip() or None,
            kind=(params.get("kind") or "").strip() or None,
            view=(params.get("view") or "").strip() or None,
            source=(params.get("source") or "").strip() or None,
            agentlog=agentlog_id,
            errors=errors_only,
            since_hours=since_hours,
            limit=limit,
        )

        filter_bits = [f"since={since_hours}h"]
        for key in ("verb", "kind", "view", "source"):
            if params.get(key):
                filter_bits.append(f"{key}={params[key]!r}")
        if agentlog_id is not None:
            filter_bits.append(f"agentlog={agentlog_id}")
        if errors_only:
            filter_bits.append("errors only")
        lines = [f"# tool_calls — {', '.join(filter_bits)}"]

        groups = out["groups"]
        if groups:
            lines += ["", "## groups — verb kind view: calls (errors)"]
            for g in groups[:_CALLS_GROUP_CAP]:
                gv = f" view={g['view']}" if g.get("view") else ""
                lines.append(
                    f"{g['verb']} {g['kind'] or '-'}{gv}: "
                    f"{g['n']} calls ({g['errors']} errors)"
                )
            if len(groups) > _CALLS_GROUP_CAP:
                lines.append(f"… +{len(groups) - _CALLS_GROUP_CAP} more groups")

        lines.append("")
        if not out["rows"]:
            lines.append(
                "no tool_calls rows match this filter in the window "
                "(the ledger starts at migration 0133; older windows "
                "still need the transcript path)."
            )
            body = "\n".join(lines)
            body += render_next_section(
                [
                    (
                        "get(kind='agentlog', id='/calls?since=720')",
                        "widen the window to the 30d retention floor",
                    ),
                ]
            )
            return interpreted(Response(body=body), readings)
        lines += [tool_ledger.call_line(r) for r in out["rows"]]
        lines.append("")
        lines.append(f"{len(out['rows'])} rows shown of {out['total']} matching")
        return interpreted(Response(body="\n".join(lines)), readings)

    def _render_sessions_mining(self, query_string: str) -> Response:
        """``id='/sessions?verb=<v>&session=<prefix>&since=<hrs>&page_size=<n>'``
        — the per-session ``tool_calls`` roll-up (block-navigation slice
        2): one line per session with hops, summed response bytes, calls
        by verb, pagination continuations (``page>1``) and the wall
        span. Same posture as ``/calls`` — all params optional, all
        bound."""
        from precis import tool_ledger
        from precis.handlers._interpret import interpreted
        from precis.handlers.job import _parse_logs_int
        from precis.utils.next_block import render_next_section
        from precis.utils.timeutil import as_utc

        params = dict(parse_qsl(query_string, keep_blank_values=True))
        unknown = sorted(k for k in params if k not in _SESSIONS_FILTER_KEYS)
        if unknown:
            raise BadInput(
                f"unknown /sessions filter(s): {', '.join(unknown)}",
                options=sorted(_SESSIONS_FILTER_KEYS),
                next="get(kind='agentlog', id='/sessions?since=168&page_size=50')",
            )

        since_hours = _parse_logs_int(
            params.get("since"),
            default=tool_ledger.CALLS_DEFAULT_SINCE_HOURS,
            field="since",
            example="since=168 (default) — hours to look back",
            units=True,
        )
        if since_hours <= 0:
            raise BadInput(
                f"since={since_hours} must be a positive number of hours",
                next="since=168 (default) — hours to look back",
            )
        since_hours = min(since_hours, tool_ledger.CALLS_MAX_SINCE_HOURS)

        readings: list[str] = []
        raw_ps, raw_lim = params.get("page_size"), params.get("limit")
        if raw_ps not in (None, "") and raw_lim not in (None, "") and raw_ps != raw_lim:
            raise BadInput(
                f"conflicting page_size={raw_ps!r} and limit={raw_lim!r}",
                next="pass one — page_size= is the canonical name",
            )
        if raw_lim not in (None, "") and raw_ps in (None, ""):
            readings.append("'limit' read as 'page_size'")
        limit = _parse_logs_int(
            raw_ps if raw_ps not in (None, "") else raw_lim,
            default=tool_ledger.CALLS_DEFAULT_LIMIT,
            field="page_size",
            example="page_size=100 (default), cap 200",
        )
        if limit <= 0:
            raise BadInput(
                f"page_size={limit} must be positive",
                next="page_size=100 (default), cap 200",
            )
        limit = min(limit, tool_ledger.CALLS_MAX_LIMIT)

        verb = (params.get("verb") or "").strip() or None
        session = (params.get("session") or "").strip() or None
        out = tool_ledger.sessions_rollup(
            self.store,
            verb=verb,
            session=session,
            since_hours=since_hours,
            limit=limit,
        )

        filter_bits = [f"since={since_hours}h"]
        if verb:
            filter_bits.append(f"verb={verb!r}")
        if session:
            filter_bits.append(f"session={session!r}*")
        lines = [f"# tool_calls sessions — {', '.join(filter_bits)}", ""]
        if not out["rows"]:
            if verb or session:
                lines.append("no sessions matched the filter")
            else:
                lines.append(
                    "no sessions in the window — the ledger keys sessions by "
                    "session_id (migration 0209); CLI and in-process calls "
                    "carry none and correlate by agentlog_id instead."
                )
            body = "\n".join(lines)
            body += render_next_section(
                [
                    (
                        "get(kind='agentlog', id='/sessions?since=720')",
                        "widen the window to the 30d retention floor",
                    ),
                ]
            )
            return interpreted(Response(body=body), readings)
        for r in out["rows"]:
            verbs = " ".join(f"{v}×{n}" for v, n in sorted(r["calls_by_verb"].items()))
            first = as_utc(r["first_ts"])
            last = as_utc(r["last_ts"])
            span = _fmt_span(int(r["span_seconds"] or 0))
            window = (
                f"{first.strftime('%H:%M')}→{last.strftime('%H:%M')}Z"
                if first is not None and last is not None
                else "?"
            )
            err_bit = f" · {r['errors']} err" if r["errors"] else ""
            lines.append(
                f"{r['session_id']} — {r['hops']} hops · "
                f"{r['resp_bytes']:,} B · {verbs} · "
                f"{r['continuations']} continuation(s){err_bit} · "
                f"{window} ({span})"
            )
        lines.append("")
        lines.append(f"{len(out['rows'])} sessions shown of {out['total']}")
        return interpreted(Response(body="\n".join(lines)), readings)

    def _render_one(self, ref, tags):
        """Default render + the triage fields a run's meta carries.

        A run's outcome lives in ``meta`` (``status``/``error`` from
        ``finalize_log``, ``model``, the salvaged ``result`` text), and the
        base render hid all of it — a failed quest_tick's agentlog read as
        just a title, which is exactly the "no queryable surface" wall the
        doctor hit for weeks on gr244061 while the offending-output excerpt
        sat on the row. The named triage keys render first; every other
        small key then renders generically (gr514912 — a new meta field
        must not need a manual opt-in to show), bounded by
        ``_EXTRA_META_CAP`` / ``_META_VALUE_CHARS``, with bulky keys and
        any overflow named on a ``view='raw'`` pointer line.
        """
        body = super()._render_one(ref, tags)
        meta = ref.meta or {}
        lines: list[str] = []
        shown: set[str] = set()
        for key in _TRIAGE_KEYS:
            val = meta.get(key)
            if val:
                lines.append(f"{key}: {_meta_value(val)}")
                shown.add(key)
        extras = sorted(
            k for k in meta if k not in shown and k not in _BULKY_KEYS and meta[k]
        )
        for key in extras[:_EXTRA_META_CAP]:
            lines.append(f"{key}: {_meta_value(meta[key])}")
            shown.add(key)
        result = meta.get("result")
        if isinstance(result, str) and result:
            excerpt = result[:400]
            more = f" … ({len(result)} chars total, view='raw' for all)"
            lines.append(f"result: {excerpt}{'' if len(result) <= 400 else more}")
            shown.add("result")
        hidden = sorted(k for k in meta if k not in shown and meta[k])
        if hidden:
            lines.append(
                f"meta: +{len(hidden)} keys ({', '.join(hidden)}) — view='raw'"
            )
        if lines:
            body += "\n\n" + "\n".join(lines)
        return body


__all__ = ["AgentLogHandler"]
