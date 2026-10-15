"""AlertHandler — read / triage side of ``kind='alert'``.

Numeric-id ref kind for machine-detected operational / health
conditions (worker spin loops, orphaned todos, stalled recurrings, …).
Subclasses :class:`NumericRefHandler` for the shared CRUD shape.

Alerts are *produced* by background passes through
:mod:`precis.alerts` (``raise_alert`` / ``resolve_stale_alerts``), not
by agents — so this handler intentionally omits ``put``. What it offers
the agent surface is the read / triage half:

    - get(kind='alert', id=N)             — read one alert + its tags
    - get(kind='alert', id=N, view='detail'/'full')
                                           — the triage-tick shape: body +
                                             severity/source/state +
                                             fingerprint/seen_count +
                                             lifecycle timestamps + links,
                                             in one call ('full' is an
                                             alias of 'detail')
    - get(kind='alert', id=N, view='links'/'log'/'raw')
                                           — generic numeric-ref views
    - get(kind='alert', id='/recent')     — recent alerts (open + resolved)
    - get(kind='alert', id='/open')       — currently-open alerts only
    - get(kind='alert', id='/rules')      — the registered failure-id
                                             catalogue (precis.alert_ids):
                                             every rule with its meaning,
                                             budget, owning service, triage
    - get(kind='alert', id='/health')     — the per-subsystem health panel
                                             (health_digest's last eval,
                                             freshness re-probed live) —
                                             the first call for any "is X
                                             healthy?" question
    - get(kind='alert', id='<source>/<fingerprint>')
                                           — one failure id: its rule, open
                                             state, seen_count, first/last
                                             seen, recurrence, and (for a
                                             watchdog id) the check's
                                             snapshot verdict. Not-open is
                                             a well-formed answer, never
                                             NotFound
    - search(kind='alert', q=...)         — lexical search over alert titles
    - tag(id=N, add=['ack:6h'])      — ack: silence an open alert for a
                                        TTL (ack:<N>m|h|d, ≤90d) via
                                        alerts.ack_alert; remove=['ack']
                                        clears it. Not stored as a tag.
    - tag(id=N, add/remove=[...])    — reclassify (resolve via
                                        add=['alert-state:resolved'],
                                        remove=['alert-state:open'];
                                        the handler syncs the
                                        ``resolved_at`` column with the
                                        state tag in the same tx)
    - link(id=N, target='kind:id')   — relate an alert to a ref
    - delete(id=N)                   — soft-delete (history pruning)

Unlike memory, alerts are NOT embedded (``emits_card`` stays False):
they're surfaced by the ``/alerts`` web tab and direct queries, never
by semantic search. See :mod:`precis.alerts` for the lifecycle.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from precis import alert_ids
from precis.alerts import (
    STATE_OPEN,
    STATE_RESOLVED,
    ack_alert,
    alert_history,
    sync_resolved_at_with_tags,
    unack_alert,
)
from precis.errors import BadInput, Unsupported
from precis.handlers._numeric_ref import _BASE_VIEWS, NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import Ref, Tag
from precis.utils.next_block import render_next_section

#: Views this kind adds on top of the base ``links``/``log``/``raw``.
#: ``full`` is an alias of ``detail`` — both were observed as recurring
#: doctor/health-digest guesses (gr259632: 26 ``view='detail'`` + 6
#: ``view='full'`` calls a tick, hit ``[error:Unsupported]``, then
#: re-guessed the same shape next tick since each tick starts a fresh
#: context). The alias lives here (AlertHandler), not on the shared
#: base — other numeric-ref kinds keep their existing view set.
_DETAIL_VIEWS: tuple[str, ...] = ("detail", "full")

#: ``view='context'`` (mcp-introspection gap closures 2026-10-14,
#: gr484235): the detail view + a bounded ``worker_logs`` window around
#: the alert's first/last seen — the read alert triage used to need
#: raw SQL for. ``worker_logs`` rows are masked line-by-line; secrets
#: never echo (same rule alerts themselves follow in raise_alert).
_CONTEXT_LINES = 80
_CONTEXT_MARGIN_MIN = 10
_CONTEXT_LINE_CHARS = 220


#: ``tag(kind='alert', add=['ack:6h'])`` (gr489245): an ack is a TTL, not a
#: mute button, so it is capped like the web tab's (``_MAX_ACK_DAYS``).
_ACK_RE = re.compile(r"ack:(\d+)([mhd])")
_ACK_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
_ACK_MAX = timedelta(days=90)
_ACK_FORMS = (
    "Accepted: add=['ack:<N>m|h|d'] (e.g. ack:30m, ack:6h, ack:7d; "
    "1 minute to 90 days) to ack, remove=['ack'] to clear."
)


def _is_ack(tag: str) -> bool:
    return tag.strip().lower() == "ack" or tag.strip().lower().startswith("ack:")


def _parse_ack(tag: str) -> timedelta:
    """``ack:6h`` → 6 hours; anything else refuses with the accepted forms."""
    m = _ACK_RE.fullmatch(tag.strip().lower())
    if m is None:
        raise BadInput(f"bad ack duration {tag!r}. {_ACK_FORMS}")
    try:
        ttl = timedelta(**{_ACK_UNITS[m.group(2)]: int(m.group(1))})
    except OverflowError:
        ttl = _ACK_MAX + _ACK_MAX
    if ttl <= timedelta(0) or ttl > _ACK_MAX:
        raise BadInput(f"ack duration {tag!r} out of range. {_ACK_FORMS}")
    return ttl


def _is_open(store: Any, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (ref_id, STATE_OPEN),
        ).fetchone()
    return row is not None


def _acked_until(ref: Ref) -> datetime | None:
    """The ack expiry if ``meta.acked_until`` is still in the future."""
    raw = (ref.meta or {}).get("acked_until")
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until if until > datetime.now(UTC) else None


#: The resolved budget in a freshness check's detail ("… (budget 170h, …)").
_BUDGET_RE = re.compile(r"\(budget (\d+(?:\.\d+)?h)\b")


def _fingerprint_vars(subject_pattern: str, fingerprint: str) -> dict[str, str]:
    """Extract ``{var}`` values from a concrete fingerprint.

    The rule's ``subject`` pattern (``dead-worker:{host}:{process}``)
    tells us WHICH fingerprint segments carry the worker's host/process
    — the alert stores no host column, so this is how a
    ``dead-worker:caspar:precis-worker-agent`` id recovers its process
    scope. Non-last vars match ``[^:/]+``; the last var takes the rest
    (fingerprints may carry '/' — ``pass-dead:caspar/precis-worker/x``).
    """
    if not subject_pattern or not fingerprint or "{" not in subject_pattern:
        return {}
    parts: list[str] = []
    names: list[str] = []
    pos = 0
    for m in re.finditer(r"\{([^{}]+)\}", subject_pattern):
        parts.append(re.escape(subject_pattern[pos : m.start()]))
        parts.append("%s")  # group placeholder, filled below
        names.append(m.group(1))
        pos = m.end()
    parts.append(re.escape(subject_pattern[pos:]))
    if not names:
        return {}
    # Placeholders sit at odd indices 1, 3, 5, … (one per var).
    last_var_part = len(parts) - 2
    for i in range(1, len(parts), 2):
        parts[i] = "(.*)" if i == last_var_part else "([^:/]+)"
    match = re.match("".join(parts) + "$", fingerprint)
    if match is None:
        return {}
    return {name: g for name, g in zip(names, match.groups()) if g}


def _worker_logs(
    store: Any, clauses: list[str], params: list[Any]
) -> list[tuple[Any, str, str | None, str | None, str]]:
    """Bounded ``worker_logs`` read for ``view='context'`` — newest
    first, capped by the caller's LIMIT param. Columns returned are
    ``(ts, level, pass, process, message)``."""
    sql = (
        "SELECT ts, level, pass, process, message FROM worker_logs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY ts DESC LIMIT %s"
    )
    with store.pool.connection() as conn:
        return [
            (r[0], str(r[1]), r[2], r[3], str(r[4] or ""))
            for r in conn.execute(sql, params).fetchall()
        ]


class AlertHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="alert",
        title="Alert",
        description=(
            "Machine-detected operational / health condition — worker "
            "spin loops, orphaned todos, stalled recurrings, stale "
            "claims. Numeric id; deduped on meta.fingerprint; lifecycle "
            "via alert-state: open tags; source + severity via "
            "alert-source: / severity: tags. Produced by background "
            "passes, not hand-authored. Not embedded."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # No put: alerts are raised by workers via precis.alerts.raise_alert,
        # never hand-authored through the agent surface.
        supports_put=False,
        supports_edit=False,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "alert"
    sense: ClassVar[str] = "alert"
    #: get/tag/link take ``id=[...]`` (summary get, one-transaction tag/fan-in link).
    batch_ids: ClassVar[bool] = True

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        return ("recent", "open", "rules", "health")

    def _list_view(self, view: str) -> Response | None:
        if view == "open":
            return self._render_open()
        if view == "rules":
            return self._render_rules()
        if view == "health":
            return self._render_health()
        return super()._list_view(view)

    def _render_rules(self) -> Response:
        """``/rules``: the failure-id catalogue, grouped by source."""
        from precis.workers.registry import SERVICES_BY_NAME

        rules = alert_ids.ALERT_RULES
        out = [
            f"# alert rules — {len(rules)} registered failure ids",
            "id = <source>/<fingerprint>; {var} is a per-instance subject. "
            "One id: get(kind='alert', id='<source>/<fingerprint>') · "
            "live states: get(kind='alert', id='/health')",
        ]
        by_source: dict[str, list[alert_ids.AlertRule]] = {}
        for r in rules:
            by_source.setdefault(r.rule, []).append(r)
        for source, group in by_source.items():
            out += ["", f"## {source}"]
            for r in group:
                line = f"- {r.subject or '(singleton)'} [{r.severity}"
                if r.budget:
                    line += f", budget {r.budget}"
                line += f"] — {r.one_line}"
                spec = SERVICES_BY_NAME.get(r.service) if r.service else None
                if spec is not None:
                    line += f" · service {spec.name}"
                    if spec.doc_skill:
                        line += f" ({spec.doc_skill})"
                if r.triage:
                    line += f" · triage: {r.triage}"
                out.append(line)
        return Response(body="\n".join(out))

    def _render_health(self) -> Response:
        """``/health``: the agent-facing per-subsystem panel."""
        from precis.workers.health_digest import health_panel

        panel = health_panel(self.store)
        age = panel["snapshot_age_hours"]
        head = "# health panel — "
        head += (
            f"snapshot {age:.1f}h old ({panel['computed_at']:%Y-%m-%dT%H:%MZ})"
            if age is not None and panel["computed_at"] is not None
            else "no health_digest eval yet (snapshot absent)"
        )
        sev = panel["open_alerts"]
        head += " · open alerts: " + (
            ", ".join(f"{n} {s}" for s, n in sorted(sev.items())) if sev else "none"
        )
        out = [
            head,
            "A check's verdict is the truth for 'is X healthy?' — not process "
            "presence (lazily-loaded services idle-unload by design). Follow an "
            "id with get(kind='alert', id='<id>') — its rule, state and detail.",
        ]
        crit = panel.get("open_critical") or []
        if crit:
            out += ["", f"## open critical ({sev.get('critical', len(crit))})"]
            out += [f"- al{a['ref_id']} {a['title']}" for a in crit]
            if sev.get("critical", 0) > len(crit):
                out.append("- more: get(kind='alert', id='/open')")
        checks = panel["checks"]
        for status, title in (("stale", "stale"), ("unknown", "unknown"), ("ok", "ok")):
            rows = [c for c in checks if c.get("status") == status]
            if not rows:
                continue
            out += ["", f"## {title} ({len(rows)})"]
            for c in rows:
                age_h = c.get("age_hours")
                age_s = f", {age_h:.1f}h" if isinstance(age_h, (int, float)) else ""
                live = " (live)" if c.get("live") else ""
                # Render diet (2026-10-14): the ok block is the panel's fat
                # (~40 checks of boilerplate detail ≈ 80% of the payload).
                # ok rows keep id + severity + age; the check's detail is one
                # single-id read away. stale/unknown rows keep detail — that
                # is the actionable text.
                if status == "ok":
                    # gr489193: an age without its budget reads as "late".
                    m = _BUDGET_RE.search(str(c.get("detail") or ""))
                    if age_s and m:
                        age_s += f" of {m.group(1)}"
                    out.append(f"- {c['id']} [{c['severity']}{age_s}]{live}")
                else:
                    out.append(
                        f"- {c['id']} [{c['severity']}{age_s}]{live} {c['detail']}"
                    )
        return Response(body="\n".join(out))

    # ── one failure id: <source>/<fingerprint> ──────────────────────

    def _render_failure_id(self, id_str: str) -> Response:
        """``get(kind='alert', id='<source>/<fingerprint>')`` — the
        deterministic single-id read (``alert_history``) joined to its
        registered rule and, for a ``watchdog:`` id, the check's snapshot
        verdict. "Not open" is a well-formed answer rather than NotFound,
        because "is it broken?" must be answerable "no"."""
        from precis.workers.registry import SERVICES_BY_NAME

        source, fingerprint = alert_ids.split_id(id_str)
        if not fingerprint:
            raise Unsupported(
                f"alert id {id_str!r} needs both halves: <source>/<fingerprint>",
                next="get(kind='alert', id='/rules') lists every registered id",
            )
        hist = alert_history(self.store, source=source, fingerprint=fingerprint)
        rule = alert_ids.resolve(source, fingerprint)
        state = hist["state"]
        out = [f"# {id_str} [{'open' if state == 'open' else 'not open'}]"]
        if rule is None:
            out.append("rule: UNREGISTERED — not in precis.alert_ids (see /rules)")
        else:
            line = f"rule: {rule.id} — {rule.one_line} · severity {rule.severity}"
            if rule.budget:
                line += f" · budget {rule.budget}"
            out.append(line)
            spec = SERVICES_BY_NAME.get(rule.service) if rule.service else None
            if spec is not None:
                svc = f"service: {spec.name} — {spec.one_line}".rstrip(" —")
                svc += f" (logs handler {spec.log_handler}"
                svc += f", skill {spec.doc_skill})" if spec.doc_skill else ")"
                out.append(svc)
            if rule.triage:
                out.append(f"triage: {rule.triage}")
        out.append("")
        out.append(
            f"state: {state} · recurrence: {hist['recurrence']} row(s)"
            + (
                f" · first seen {hist['first_seen']:%Y-%m-%dT%H:%MZ}"
                if hist["first_seen"]
                else ""
            )
            + (
                f" · last seen {hist['last_seen']:%Y-%m-%dT%H:%MZ}"
                if hist["last_seen"]
                else ""
            )
            + (
                f" · last resolved {hist['last_resolved_at']:%Y-%m-%dT%H:%MZ}"
                if hist["last_resolved_at"]
                else ""
            )
        )
        live = hist["open"]
        if live:
            out += [
                f"open: alert {live['ref_id']} · severity {live['severity']} · "
                f"seen_count {live['seen_count']} · since "
                f"{live['created_at']:%Y-%m-%dT%H:%MZ} · updated "
                f"{live['updated_at']:%Y-%m-%dT%H:%MZ}",
                live["title"] or "",
            ]
            if live.get("detail"):
                out.append(live["detail"])
        if source.startswith("watchdog:"):
            out.append(self._snapshot_verdict_line(id_str))
        body = "\n".join(out)
        if live:
            body += render_next_section(
                [
                    (
                        f"get(kind='alert', id={live['ref_id']}, view='detail')",
                        "the open alert's triage shape",
                    )
                ]
            )
        return Response(body=body)

    def _snapshot_verdict_line(self, id_str: str) -> str:
        from precis.workers.health_digest import health_panel

        panel = health_panel(self.store)
        row = next((c for c in panel["checks"] if c.get("id") == id_str), None)
        if row is None:
            return "check: not in the last health_digest snapshot"
        age = panel["snapshot_age_hours"]
        src = (
            "live"
            if row.get("live")
            else (f"snapshot {age:.1f}h old" if age is not None else "snapshot")
        )
        return f"check ({src}): {row['status']} — {row['detail']}"

    def _render_open(self) -> Response:
        """Currently-open alerts, recency-ordered."""
        refs = self.store.list_refs(kind=self.kind, tags=[STATE_OPEN], limit=200)
        refs = sorted(refs, key=lambda r: r.updated_at, reverse=True)
        if not refs:
            return Response(body="no open alerts — all clear.")
        header = f"# {len(refs)} open alert{'' if len(refs) == 1 else 's'}"
        body = f"{header}\n{self._render_hits_table(refs)}"
        # An ack keeps the alert open, so it stays listed; name the acked ones
        # the way /alerts dims them, with their expiry.
        acked = [(r.id, _acked_until(r)) for r in refs]
        acked_lines = [
            f"- al{ref_id} acked until {until.strftime('%Y-%m-%d %H:%M')}Z"
            for ref_id, until in acked
            if until is not None
        ]
        if acked_lines:
            body += "\nacked (left out of the badge and remediation until then):\n"
            body += "\n".join(acked_lines)
        return Response(body=body)

    # ── get: base views + view='detail' (alias 'full') ─────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        host: str | None = None,
        process: str | None = None,
        log_pass: str | None = None,
        minutes: int | str | None = None,
        **_kw: Any,
    ) -> Response:
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        # gr259632: recurring health-digest/nursery ticks repeatedly
        # guessed view='detail' / view='full' on a concrete id and hit
        # the generic [error:Unsupported] every tick (each tick is a
        # fresh context, so the guess never got corrected). Mirrors
        # MemoryHandler.get's view='argument' dispatch shape — a
        # concrete-id-only extra view layered in front of the base
        # links/log/raw set.
        if isinstance(id, list):
            return super().get(id=id, view=view, q=q, **_kw)
        # A composite failure id (<source>/<fingerprint>) is checked
        # BEFORE _coerce_id: it can never start with '/' (sources never
        # contain one), so it cannot collide with the list-view path.
        if isinstance(id, str) and "/" in id and not id.startswith("/"):
            return self._render_failure_id(id)
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if concrete and view in _DETAIL_VIEWS:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return self._render_detail_view(ref)
        if concrete and view == "context":
            ref = self._resolve_live_ref(self._coerce_id(id))
            return self._render_context_view(
                ref,
                host=host,
                process=process,
                log_pass=log_pass,
                minutes=minutes,
            )
        if concrete and view is not None and view not in _BASE_VIEWS:
            raise Unsupported(
                f"unknown view {view!r} for kind='alert'",
                options=[*_DETAIL_VIEWS, "context", *_BASE_VIEWS],
                next=(
                    "view='detail' (triage shape: body + severity/source/"
                    "state + fingerprint/seen_count + timestamps + links; "
                    "'full' is an alias) · view='context' (detail + "
                    "worker_logs window) · links, log, raw (generic)"
                ),
            )
        return super().get(id=id, view=view, q=q, **_kw)

    # ── get view='context' — alert + worker_logs window ─────────

    def _render_context_view(
        self,
        ref: Ref,
        *,
        host: str | None,
        process: str | None,
        log_pass: str | None,
        minutes: int | str | None,
    ) -> Response:
        """``view='context'``: the detail view + a bounded
        ``worker_logs`` slice around the alert's first/last seen.

        The log scope resolves in layers: the registered rule's
        ``service`` → ``log_handler`` gives the ``pass`` to read (the
        source's first segment is the fallback for unregistered ids);
        ``host``/``process`` come from ``args=`` overrides first, then
        ``meta``, then the fingerprint's ``{host}``/``{process}``
        subject vars. Every emitted line is ``mask_secrets``-filtered —
        alert context must never echo a secret.
        """
        from datetime import timedelta

        from precis.utils.secret_scan import mask_secrets
        from precis.workers.registry import SERVICES_BY_NAME

        meta = ref.meta or {}
        source = ref.alert_source or meta.get("alert_source") or ""
        fingerprint = ref.fingerprint or meta.get("fingerprint") or ""
        rule = alert_ids.resolve(source, fingerprint) if source else None

        passes: list[str] = []
        if log_pass:
            passes = [log_pass]
        else:
            if rule is not None and rule.service:
                spec = SERVICES_BY_NAME.get(rule.service)
                if spec is not None:
                    passes.append(spec.log_handler)
            guess = source.split(":")[0].strip()
            if guess and guess not in passes:
                passes.append(guess)

        fvars = _fingerprint_vars(rule.subject if rule else "", fingerprint)
        sel_host = host or meta.get("host") or fvars.get("host")
        sel_process = process or meta.get("process") or fvars.get("process")

        try:
            margin = int(minutes) if minutes is not None else _CONTEXT_MARGIN_MIN
        except (TypeError, ValueError):
            margin = _CONTEXT_MARGIN_MIN
        margin = max(1, min(margin, 1440))
        lo = ref.created_at - timedelta(minutes=margin)
        hi = (ref.resolved_at or ref.updated_at) + timedelta(minutes=margin)

        clauses = ["ts BETWEEN %s AND %s"]
        params: list[Any] = [lo, hi]
        if passes:
            clauses.append("pass = ANY(%s)")
            params.append(passes)
        if sel_host:
            clauses.append("host = %s")
            params.append(str(sel_host))
        if sel_process:
            clauses.append("process = %s")
            params.append(str(sel_process))
        rows = _worker_logs(self.store, clauses, [*params, _CONTEXT_LINES])

        base = self._render_detail_view(ref)
        out = [base.body, ""]
        scope = " · ".join(
            f"{k} {v!r}"
            for k, v in (
                ("pass", "+".join(passes) if passes else "any"),
                ("host", sel_host or "any"),
                ("process", sel_process or "any"),
            )
        )
        out.append(
            f"## worker_logs ({scope}; {lo:%Y-%m-%dT%H:%MZ} → {hi:%H:%MZ}, ±{margin}m)"
        )
        if not rows:
            out.append(
                "(no rows in window — override with "
                "args={'host':…,'process':…,'log_pass':…,'minutes':…})"
            )
        for ts, level, wpass, wproc, wmsg in rows:
            msg = mask_secrets(wmsg, warn=False)
            if len(msg) > _CONTEXT_LINE_CHARS:
                msg = msg[: _CONTEXT_LINE_CHARS - 1] + "…"
            out.append(f"  {ts:%H:%M:%S} {level} {wproc or wpass or '-'}: {msg}")
        return Response(body="\n".join(out))

    def _render_detail_view(self, ref: Ref) -> Response:
        """``view='detail'``/``'full'``: the one-call triage shape.

        Everything a doctor/health-digest tick actually wants — body,
        lifecycle state, dedup identity, timestamps, and the link graph
        — instead of stitching together a bare ``get`` (title + tags
        only) with ``view='raw'`` (meta dump) and ``view='links'``.
        Terse and data-dense on purpose (MCP payload, not prose).
        """
        tags = self.store.tags_for(ref.id)
        meta = ref.meta or {}
        is_open = any(t.namespace == "open" and t.value == STATE_OPEN for t in tags)
        state = "open" if is_open else "resolved" if ref.resolved_at else "unknown"

        out = [f"# alert {ref.id} [{state}]", "", ref.title]
        detail = meta.get("detail")
        if detail:
            out += ["", detail]

        out.append("")
        fields: list[tuple[str, Any]] = [
            ("severity", meta.get("severity")),
            ("source", ref.alert_source or meta.get("alert_source")),
            ("fingerprint", ref.fingerprint or meta.get("fingerprint")),
            ("seen_count", meta.get("seen_count")),
            ("subject_ref_id", meta.get("subject_ref_id")),
            ("created_at", ref.created_at),
            ("updated_at", ref.updated_at),
            ("resolved_at", ref.resolved_at),
        ]
        out += [f"{k}: {v}" for k, v in fields if v is not None]

        if tags:
            out += ["", "tags: " + " ".join(str(t) for t in tags)]

        body = "\n".join(out)
        body += self._render_links_section(ref)
        return Response(body=body)

    # ── tag-verb ack (gr489245) ─────────────────────────────────────

    def tag(self, **kw: Any) -> Response:
        """``add=['ack:6h']`` silences an open alert for that long through
        :func:`precis.alerts.ack_alert` (``meta.acked_until``; the row stays
        open and nothing is stored as a tag); ``remove=['ack']`` clears it.
        Every other tag goes through the generic tag path. Takes ``**kw``
        (``id``, ``add``, ``remove``, ...) to match the verb protocol.
        """
        id: str | int | list[str | int] = kw.pop("id")
        add: list[str] | None = kw.pop("add", None)
        remove: list[str] | None = kw.pop("remove", None)
        acks = [s for s in add or [] if _is_ack(s)]
        unacks = [s for s in remove or [] if _is_ack(s)]
        if not acks and not unacks:
            return super().tag(id=id, add=add, remove=remove, **kw)
        if len(acks) > 1:
            raise BadInput(f"one ack per call, got {acks}. {_ACK_FORMS}")
        if acks and unacks:
            raise BadInput(f"add=['ack:…'] and remove=['ack'] together. {_ACK_FORMS}")
        ttl = _parse_ack(acks[0]) if acks else None
        ids = id if isinstance(id, list) else [id]
        ref_ids = [self._coerce_id(i) for i in ids]
        for ref_id in ref_ids:
            self._resolve_live_ref(ref_id)
            if not _is_open(self.store, ref_id):
                raise BadInput(
                    f"alert {ref_id} is not open; only an open alert takes an ack"
                )
        rest_add = [s for s in add or [] if not _is_ack(s)]
        rest_remove = [s for s in remove or [] if not _is_ack(s)]
        lines: list[str] = []
        if rest_add or rest_remove:
            lines.append(
                super().tag(id=id, add=rest_add, remove=rest_remove, **kw).body
            )
        for ref_id in ref_ids:
            if ttl is None:
                unack_alert(self.store, ref_id)
                lines.append(f"alert {ref_id}: ack cleared; it counts as live again.")
                continue
            until = datetime.now(UTC) + ttl
            if not ack_alert(self.store, ref_id, until=until, acked_by="agent"):
                raise BadInput(f"alert {ref_id} is no longer open; not acked")
            lines.append(
                f"alert {ref_id}: acked until {until.strftime('%Y-%m-%d %H:%M')}Z "
                f"({acks[0]}); it stays open (marked acked on /open) and is left "
                "out of the nav badge, the backlog-rot check and remediation "
                "until then."
            )
        ack_hint = (
            (f"tag(kind='alert', id={ref_ids[0]}, add=['ack:6h'])", "ack it again")
            if ttl is None
            else (
                f"tag(kind='alert', id={ref_ids[0]}, remove=['ack'])",
                "clear the ack",
            )
        )
        hints = [
            ack_hint,
            (
                f"tag(kind='alert', id={ref_ids[0]}, "
                "add=['alert-state:resolved'], remove=['alert-state:open'])",
                "resolve it instead",
            ),
        ]
        return Response(body="\n".join(lines) + render_next_section(hints))

    # ── tag-verb lifecycle sync ─────────────────────────────────────

    _LIFECYCLE_TAGS: ClassVar[frozenset[str]] = frozenset({STATE_OPEN, STATE_RESOLVED})

    def _after_tag_mutation(
        self,
        ref_id: int,
        added: list[Tag],
        removed: list[Tag],
        *,
        conn: Any,
    ) -> None:
        """Keep ``resolved_at`` in step with an ``alert-state`` tag edit.

        The 0099 dedup unique index keys off ``resolved_at IS NULL``,
        not the tag, so a tag-only resolve would leave the open slot
        occupied and the next ``raise_alert`` of a still-live condition
        would hit a unique violation. Same transaction as the tag
        writes, so the pair can't drift apart.
        """
        # ``alert-state:`` is a mutually exclusive axis (gr479086): adding
        # one lifecycle value drops the other, like a closed ``STATUS:``
        # add. The tag is a lowercase open tag (the open/resolved rows are
        # keyed on it), so the replace is done here rather than by the
        # closed-prefix ``replace_prefix`` path. When one call adds both
        # values the last one wins.
        wanted = [
            t.value
            for t in added
            if t.namespace == "open" and t.value in self._LIFECYCLE_TAGS
        ]
        if wanted:
            for stale in self._LIFECYCLE_TAGS - {wanted[-1]}:
                self.store.remove_tag(ref_id, Tag.open(stale), conn=conn)
        touched = {t.value for t in (*added, *removed) if t.namespace == "open"}
        if touched & self._LIFECYCLE_TAGS:
            sync_resolved_at_with_tags(conn, ref_id, resolved_by="agent")

    def _create_ack_next_hints(self, ref_id: int) -> list[tuple[str, str]]:
        # Alerts aren't put-created through this handler, but keep the
        # base hints coherent if a future producer path reuses the ack.
        return [
            (
                f"tag(kind='alert', id={ref_id}, "
                "add=['alert-state:resolved'], remove=['alert-state:open'])",
                "mark this alert resolved",
            ),
            *super()._create_ack_next_hints(ref_id),
        ]


__all__ = ["AlertHandler"]
