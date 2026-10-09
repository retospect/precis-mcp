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
    - tag(id=N, add/remove=[...])    — ack / reclassify (resolve via
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

from typing import Any, ClassVar

from precis import alert_ids
from precis.alerts import (
    STATE_OPEN,
    STATE_RESOLVED,
    alert_history,
    sync_resolved_at_with_tags,
)
from precis.errors import Unsupported
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
            "id with get(kind='alert', id='<id>').",
        ]
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
                out.append(f"- {c['id']} [{c['severity']}{age_s}]{live} {c['detail']}")
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
        return Response(body=f"{header}\n{self._render_hits_table(refs)}")

    # ── get: base views + view='detail' (alias 'full') ─────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # gr259632: recurring health-digest/nursery ticks repeatedly
        # guessed view='detail' / view='full' on a concrete id and hit
        # the generic [error:Unsupported] every tick (each tick is a
        # fresh context, so the guess never got corrected). Mirrors
        # MemoryHandler.get's view='argument' dispatch shape — a
        # concrete-id-only extra view layered in front of the base
        # links/log/raw set.
        # A composite failure id (<source>/<fingerprint>) is checked
        # BEFORE _coerce_id: it can never start with '/' (sources never
        # contain one), so it cannot collide with the list-view path.
        if isinstance(id, str) and "/" in id and not id.startswith("/"):
            return self._render_failure_id(id)
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if concrete and view in _DETAIL_VIEWS:
            ref = self._resolve_live_ref(self._coerce_id(id))
            return self._render_detail_view(ref)
        if concrete and view is not None and view not in _BASE_VIEWS:
            raise Unsupported(
                f"unknown view {view!r} for kind='alert'",
                options=[*_DETAIL_VIEWS, *_BASE_VIEWS],
                next=(
                    "view='detail' (triage shape: body + severity/source/"
                    "state + fingerprint/seen_count + timestamps + links; "
                    "'full' is an alias) · links, log, raw (generic)"
                ),
            )
        return super().get(id=id, view=view, q=q, **_kw)

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
