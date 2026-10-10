"""FleetHandler — ``kind='fleet'``, the agent-fleet coordination rows.

One numeric ref per agent tree (``meta.type='agent'``) and one per host
(``meta.type='host'``). Reporters own the observed fields; the coordinator
owns ``assigned`` / ``slice`` / ``note``. Not embedded, not searchable.

    - put(kind='fleet', mode='report', args={'host', 'report'}) — a
      collector's ``scripts/fleet-report --json`` output (the only put)
    - edit(kind='fleet', id=…, mode='replace', args={'assigned': …}) —
      coordinator-owned fields only
    - get(kind='fleet')            — the fisheye (EXCEPTIONS / AGENTS / QUOTA)
    - get(kind='fleet', id=key|fl123) — one row

Write logic lives in :mod:`precis.fleet`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from precis import fleet
from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec, tolerates_extra_kwargs
from precis.response import Response
from precis.store import Ref, Tag
from precis.utils import handle_registry
from precis.utils.secret_scan import mask_secrets_deep


def _parse_ts(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_min(text: Any, now: datetime) -> float | None:
    ts = _parse_ts(text)
    return (now - ts).total_seconds() / 60 if ts else None


def _window_label(minutes: Any) -> str:
    if not minutes:
        return "?"
    return (
        "5h"
        if minutes <= 360
        else "week"
        if minutes >= 10000
        else f"{round(minutes / 60)}h"
    )


def _who(meta: dict[str, Any]) -> str:
    return (
        f"{meta.get('vendor')}@{meta.get('host')}/"
        f"{meta.get('project')}/{meta.get('tree')}"
    )


class FleetHandler(NumericRefHandler):
    #: machine-collected host reports, not agent prose: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="fleet",
        title="Agent fleet",
        description=(
            "Live coordination rows for the agent fleet: one per agent tree "
            "(vendor, host, project, tree; state, purpose, last activity, "
            "context fill, exceptions, attach line) and one per host (report "
            "time, quota). Collectors push with put(mode='report'); the "
            "coordinator sets assigned/slice/note with edit(mode='replace'). "
            "get(kind='fleet') renders EXCEPTIONS / AGENTS / QUOTA; "
            "args={'project','host'} filter. Not embedded, not searchable."
        ),
        supports_get=True,
        supports_search=False,
        supports_put=True,
        supports_edit=True,
        supports_delete=True,
        is_numeric=True,
        id_required=False,
        modes=("report",),
        edit_modes=("replace",),
    )

    kind: ClassVar[str] = "fleet"
    sense: ClassVar[str] = "fleet row"

    # ── put: report ingestion only ──────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        mode: str | None = None,
        host: str | None = None,
        report: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if mode != "report":
            raise BadInput(
                "put(kind='fleet') only takes mode='report'; rows are created "
                "by collector reports, never by hand",
                next=(
                    "put(kind='fleet', mode='report', args={'host': '<host>', "
                    "'report': <scripts/fleet-report --json output>})"
                ),
            )
        if id is not None:
            raise BadInput(
                "put(mode='report') takes no id=",
                next="edit(kind='fleet', id=N, mode='replace', args={'assigned': ...})",
            )
        if not isinstance(report, dict):
            raise BadInput(
                "mode='report' requires report= (the fleet-report --json object)",
                next="args={'host': '<host>', 'report': {...}}",
            )
        rhost = str(report.get("host") or "").strip()
        host = (host or rhost).strip()
        if rhost and host != rhost:
            raise BadInput(
                f"host={host!r} but the report is from {rhost!r}",
                next="omit host= (the report names its host) or pass the same one",
            )
        if not host:
            raise BadInput(
                "mode='report' requires host= (or report.host)",
                next="args={'host': '<host>', 'report': {...}}",
            )
        # Pane screens / transcripts can show tokens: mask, don't refuse.
        report = mask_secrets_deep(report)
        res = fleet.apply_report(self.store, host=host, report=report)
        return Response(body=res.summary(host))

    # ── edit: coordinator-owned fields ──────────────────────────────

    @tolerates_extra_kwargs
    def edit(
        self,
        *,
        id: str | int | None = None,
        mode: str = "replace",
        assigned: str | None = None,
        slice: str | None = None,
        note: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Set coordinator-owned fields (``''`` clears one)."""
        reporter = sorted(k for k in _kw if k in fleet.REPORTER_FIELDS)
        if reporter:
            raise BadInput(
                f"{reporter} are reporter-owned and refreshed by every report; "
                "edit takes only assigned, slice, note",
                next="edit(kind='fleet', id=N, mode='replace', args={'assigned': '...'})",
            )
        if _kw:
            raise BadInput(
                f"edit(kind='fleet') does not accept {sorted(_kw)!r}; "
                "it takes only assigned, slice, note",
            )
        if id is None:
            raise BadInput(
                "edit(kind='fleet') requires id=",
                next="get(kind='fleet') lists rows; edit(kind='fleet', id=<key|fl123>, ...)",
            )
        patch = {
            k: (v or None)
            for k, v in (("assigned", assigned), ("slice", slice), ("note", note))
            if v is not None
        }
        if not patch:
            raise BadInput(
                "edit(kind='fleet') needs at least one of assigned, slice, note",
                next="edit(kind='fleet', id=N, mode='replace', args={'assigned': '...'})",
            )
        ref = self._ref_for(id)
        if (ref.meta or {}).get("type") != "agent":
            raise BadInput(
                f"{ref.title!r} is not an agent row; only agent rows take "
                "coordinator fields",
            )
        self.store.update_ref(ref.id, meta_patch=patch)
        return Response(body=f"updated {', '.join(sorted(patch))} on {ref.title}")

    # ── id resolution: key, numeric id, or the fl handle ────────────

    def _ref_for(self, id: str | int) -> Ref:
        if isinstance(id, str):
            s = id.strip()
            core = s[len("fleet:") :] if s.startswith("fleet:") else s
            if not core.isdigit():
                ref = self.store.find_ref_by_meta(kind=self.kind, key="key", value=s)
                if ref is None:
                    raise NotFound(
                        f"no fleet row with key {s!r}",
                        next="get(kind='fleet') lists rows; keys read vendor:host:project:tree",
                    )
                return ref
        return self._resolve_live_ref(self._coerce_id(id))

    # ── get ─────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        project: str | None = None,
        host: str | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, list):
            return super().get(id=id, view=view, q=q, **_kw)  # refused: no batch form
        if id is None and view is None:
            return Response(body=self._render_fisheye(project=project, host=host))
        if isinstance(id, str) and not id.startswith("/"):
            return super().get(id=self._ref_for(id).id, view=view, q=q, **_kw)
        return super().get(id=id, view=view, q=q, **_kw)

    def _list_view(self, view: str) -> Response | None:
        return None

    def _supported_list_views(self) -> tuple[str, ...]:
        return ()

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:
        meta = ref.meta or {}
        handle = handle_registry.try_format(self.kind, ref.id) or f"fleet:{ref.id}"
        lines = [f"# {handle} — {meta.get('key', ref.title)}"]
        skip = {"key"}
        for k, v in meta.items():
            if k in skip or v in (None, "", [], {}):
                continue
            lines.append(f"{k}: {v}")
        return "\n".join(lines)

    def _render_fisheye(
        self,
        *,
        project: str | None = None,
        host: str | None = None,
        now: datetime | None = None,
    ) -> str:
        now = now or datetime.now(UTC)
        refs = self.store.list_refs(kind=self.kind, limit=5000)
        hosts: dict[str, dict[str, Any]] = {}
        agents: list[dict[str, Any]] = []
        for r in refs:
            m = dict(r.meta or {})
            if host and m.get("host") != host:
                continue
            if m.get("type") == "host":
                hosts[str(m.get("host"))] = m
            elif m.get("type") == "agent":
                if project and m.get("project") != project:
                    continue
                m["_id"] = r.id
                agents.append(m)

        stale_hosts: dict[str, float | None] = {}
        for name, hm in hosts.items():
            age = _age_min(hm.get("reported_at"), now)
            if age is None or age * 60 > fleet.STALE_HOST_S:
                stale_hosts[name] = age

        def is_stale(m: dict[str, Any]) -> bool:
            h = str(m.get("host"))
            return h in stale_hosts or h not in hosts

        agents.sort(
            key=lambda m: (
                str(m.get("project")),
                str(m.get("tree")),
                str(m.get("vendor")),
            )
        )

        lines = ["EXCEPTIONS"]
        n_exc = 0
        for m in agents:
            codes = m.get("exceptions") or []
            if not codes:
                continue
            tail = f"   attach: {m['attach']}" if m.get("attach") else ""
            lines.append(f"  {_who(m)}  {','.join(codes)}{tail}")
            n_exc += 1
        for name, age in sorted(stale_hosts.items()):
            when = f"{age:.0f}m ago" if age is not None else "never"
            lines.append(f"  host {name}  stale (last report {when})")
            n_exc += 1
        if not n_exc:
            lines.append("  none")

        lines.append("AGENTS")
        for m in agents:
            quiet = _age_min(m.get("last_active"), now)
            q = f"{quiet:.0f}m" if quiet is not None else "-"
            ctx = f"{m['ctx_pct']:.0f}%" if m.get("ctx_pct") is not None else "-"
            purpose = str(m.get("purpose") or "")[:40]
            stale = " · STALE" if is_stale(m) else ""
            lines.append(
                f"  {_who(m)} · {m.get('state')}{stale} · {q} · {purpose} · {ctx}"
            )
        if not agents:
            lines.append("  none")

        quota: dict[tuple[str, str], Any] = {}
        for hm in sorted(hosts.values(), key=lambda h: str(h.get("reported_at"))):
            for vendor, limits in (hm.get("quota") or {}).items():
                for w in (limits or {}).values():
                    if isinstance(w, dict) and w.get("used_percent") is not None:
                        quota[(vendor, _window_label(w.get("window_minutes")))] = w[
                            "used_percent"
                        ]
        if quota:
            lines.append(
                "QUOTA   "
                + " · ".join(
                    f"{v} {lab} {pct:.0f}%" for (v, lab), pct in sorted(quota.items())
                )
            )
        return "\n".join(lines)


__all__ = ["FleetHandler"]
