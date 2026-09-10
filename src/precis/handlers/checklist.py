"""ChecklistHandler — Checklist-Manifesto gates for LLM agents (``checklist``
kind).

Design-of-record: ``docs/backlog/checklist-kind.md`` (slice 1: "checklist
kind core"). A checklist is a named, versioned set of **items**; any ref
(pcb, cad, se, ...) can be explicitly **assigned** one or more checklists,
and per-target, per-item **verdicts** accumulate in an append-only ledger
instead of restarting on every re-check.

* ``put(id=<name>, items=[{...}])`` creates a **local** checklist (origin
  is always ``local`` from this verb — ``origin='shipped'`` checklists and
  item revs are created only by :mod:`precis.jobs.checklist_sync`, from
  files under ``src/precis/data/checklists/``).
* ``edit(id=<name>, op=..., ...)`` covers everything else:
  ``add_item`` (insert a new item, or a new rev of an existing item —
  REJECTED when the item's current rev is ``shipped``, with a hint to add
  a local item or file a gripe instead), ``retire_item`` (same rejection
  rule), ``verdict`` (append a verdict row for one target+item),
  ``add_note``/``remove_note`` (the ``se_notes`` argument-thread shape,
  lifted to :mod:`precis.utils.notes`), ``assign``/``unassign`` (the
  target/checklist binding that makes silence honest).
* ``get(id=<name>)`` renders the checklist definition; ``get(id=<name>,
  target=<kind:id>)`` renders the **per-target status view**,
  three-valued: an item with no verdict is "not checked"; a verdict whose
  ``item_rev`` is behind the item's current rev is "stale (item
  revised)"; a verdict whose caller-supplied ``fingerprint=`` disagrees
  with the current one (when the caller supplies one — fingerprints are
  an OPAQUE caller-supplied string in this slice, never computed here)
  is "stale (target changed)"; otherwise the recorded verdict. An
  unassigned target renders "no checklist assigned", never empty-clean.
* ``search(q=...)`` matches checklist names.

NOT in this slice: any pcb-specific code, fingerprint computation, the
pcb-tapeout content, the ``checklist_clean`` evaluator, kind-default
assignments, skills. See the design doc's "Explicitly NOT in scope"
section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils.notes import NOTE_KINDS, NoteError, validate_about

_SEVERITIES: tuple[str, ...] = ("blocking", "advisory")
_DECIDABILITY: tuple[str, ...] = ("tool", "judgment")
_VERDICTS: tuple[str, ...] = ("pass", "fail", "n/a", "waived")
_EDIT_OPS: tuple[str, ...] = (
    "add_item",
    "retire_item",
    "verdict",
    "add_note",
    "remove_note",
    "assign",
    "unassign",
)
_VIEWS: tuple[str, ...] = ("page",)


@dataclass(frozen=True)
class _Target:
    """Uniform handle for a resolved checklist target — any kind, not
    just checklist's own rows."""

    id: int
    kind: str
    label: str


class ChecklistHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="checklist",
        title="Checklist",
        description=(
            "A named, versioned check ledger — Checklist-Manifesto-style "
            "argued gates. put(id=<name>, items=[{'name':..., "
            "'prevents':'the failure this catches', 'severity':"
            "'blocking'|'advisory', 'decidability':'tool'|'judgment', "
            "'phase':..., 'applies':..., 'body':...}]) creates a local "
            "checklist. edit(id=<name>, op=..., ...) covers add_item, "
            "retire_item, verdict, add_note, remove_note, assign, "
            "unassign — op='add_item'/'retire_item' on a shipped item rev "
            "is REJECTED (add a local item or file a gripe instead). "
            "get(id=<name>) renders the definition; get(id=<name>, "
            "target='<kind>:<id>') renders the per-target status view "
            "(not checked / stale / current verdict per item — an "
            "unassigned target says so, never empty-clean). "
            "search(q=...) matches checklist names. See "
            "precis-checklist-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_edit=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
        placement="artifact",
        corpus_role="none",
        views=_VIEWS,
        edit_modes=_EDIT_OPS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("checklist: store required")
        self.store = hub.store

    # ── put ──────────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        items: list[dict[str, Any]] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='checklist') requires id=<name>",
                next=(
                    "put(kind='checklist', id='my-checklist', items=["
                    "{'name': 'item-one', 'prevents': "
                    "'what breaks if this is skipped'}])"
                ),
            )
        name = str(id).strip()
        if self.store.checklist_get(name) is not None:
            raise BadInput(
                f"checklist {name!r} already exists",
                next=(
                    f"edit(kind='checklist', id={name!r}, op='add_item', "
                    "item=..., prevents=...) to add items to it"
                ),
            )
        if items is not None and not isinstance(items, list):
            raise BadInput("put(kind='checklist') items= must be a list of dicts")
        payloads = [_validate_item_payload(raw) for raw in (items or [])]

        with self.store.tx() as conn:
            row = self.store.checklist_create(name=name, origin="local", conn=conn)
            for p in payloads:
                self.store.checklist_item_add_rev(
                    checklist_id=row["id"],
                    name=p["name"],
                    rev=1,
                    phase=p["phase"],
                    severity=p["severity"],
                    decidability=p["decidability"],
                    prevents=p["prevents"],
                    applies=p["applies"],
                    body=p["body"],
                    origin="local",
                    conn=conn,
                )
        names = ", ".join(p["name"] for p in payloads) or "(none yet)"
        return Response(
            body=(
                f"created checklist {name!r} (local) with {len(payloads)} "
                f"item(s): {names}"
            ),
            ref_id=row["id"],
        )

    # ── edit ─────────────────────────────────────────────────────────────

    def edit(
        self,
        *,
        id: str | int | None = None,
        op: str | None = None,
        item: str | None = None,
        target: str | int | None = None,
        phase: str | None = None,
        severity: str | None = None,
        decidability: str | None = None,
        prevents: str | None = None,
        applies: str | None = None,
        body: str | None = None,
        verdict: str | None = None,
        evidence: dict[str, Any] | None = None,
        fingerprint: str | None = None,
        checked_by: str | None = None,
        name: str | None = None,
        note_kind: str | None = None,
        re: str | None = None,
        about: Any = None,
        origin: str = "user",
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("edit(kind='checklist') requires id=<name>")
        checklist_name = str(id).strip()
        checklist = self.store.checklist_get(checklist_name)
        if checklist is None:
            raise NotFound(f"checklist {checklist_name!r} not found")
        if op not in _EDIT_OPS:
            raise BadInput(
                f"edit(kind='checklist') op={op!r} must be one of {list(_EDIT_OPS)}"
            )
        if op == "add_item":
            return self._edit_add_item(
                checklist,
                item=item,
                target=target,
                phase=phase,
                severity=severity,
                decidability=decidability,
                prevents=prevents,
                applies=applies,
                body=body,
            )
        if op == "retire_item":
            return self._edit_retire_item(checklist, item=item)
        if op == "verdict":
            return self._edit_verdict(
                checklist,
                target=target,
                item=item,
                verdict=verdict,
                evidence=evidence,
                fingerprint=fingerprint,
                checked_by=checked_by,
            )
        if op == "add_note":
            return self._edit_add_note(
                checklist,
                target=target,
                name=name,
                note_kind=note_kind,
                body=body,
                item=item,
                re=re,
                about=about,
                origin=origin,
            )
        if op == "remove_note":
            return self._edit_remove_note(checklist, target=target, name=name)
        if op == "assign":
            return self._edit_assign(checklist, target=target)
        if op == "unassign":
            return self._edit_unassign(checklist, target=target)
        raise BadInput(f"unhandled op {op!r}")  # pragma: no cover — guarded above

    def _reject_if_shipped(
        self, checklist: dict[str, Any], item_name: str
    ) -> dict[str, Any] | None:
        """Current live rev of ``item_name``, or ``None``. Raises when the
        current rev is shipped — the write-ownership boundary: git owns
        shipped content, ``edit()`` never rewrites it."""
        current = self.store.checklist_item_current(checklist["id"], item_name)
        if current is not None and current["origin"] == "shipped":
            raise BadInput(
                f"item {item_name!r} rev {current['rev']} on checklist "
                f"{checklist['name']!r} is shipped — edit() cannot rewrite "
                "shipped content (git owns it)",
                next=(
                    "add a local item under a new name for this target's own "
                    "concern, or put(kind='gripe', ...) to propose changing "
                    "the shipped checklist"
                ),
            )
        return current

    def _edit_add_item(
        self,
        checklist: dict[str, Any],
        *,
        item: str | None,
        target: str | int | None,
        phase: str | None,
        severity: str | None,
        decidability: str | None,
        prevents: str | None,
        applies: str | None,
        body: str | None,
    ) -> Response:
        if item is None or not str(item).strip():
            raise BadInput("edit(kind='checklist', op='add_item') requires item=<name>")
        item_name = str(item).strip()
        current = self._reject_if_shipped(checklist, item_name)

        resolved_severity = severity or (current["severity"] if current else "advisory")
        if resolved_severity not in _SEVERITIES:
            raise BadInput(f"severity={severity!r} must be one of {list(_SEVERITIES)}")
        resolved_decidability = decidability or (
            current["decidability"] if current else "judgment"
        )
        if resolved_decidability not in _DECIDABILITY:
            raise BadInput(
                f"decidability={decidability!r} must be one of {list(_DECIDABILITY)}"
            )
        resolved_prevents = prevents or (current["prevents"] if current else None)
        if not resolved_prevents or not str(resolved_prevents).strip():
            raise BadInput(
                f"item {item_name!r} requires prevents= (the failure this "
                "item catches) — no failure statement, no item",
                next=(
                    "edit(kind='checklist', id=..., op='add_item', "
                    f"item={item_name!r}, prevents='what breaks if this is "
                    "skipped')"
                ),
            )
        resolved_phase = (
            phase if phase is not None else (current["phase"] if current else None)
        )
        resolved_applies = (
            applies
            if applies is not None
            else (current["applies"] if current else None)
        )
        resolved_body = (
            body if body is not None else (current["body"] if current else None)
        )
        target_ref_id = current["target_ref_id"] if current else None
        if target is not None:
            target_ref_id = self._resolve_target(target).id

        rev = self.store.checklist_item_max_rev(checklist["id"], item_name) + 1
        row = self.store.checklist_item_add_rev(
            checklist_id=checklist["id"],
            name=item_name,
            rev=rev,
            phase=resolved_phase,
            severity=resolved_severity,
            decidability=resolved_decidability,
            prevents=str(resolved_prevents).strip(),
            applies=resolved_applies,
            body=resolved_body,
            origin="local",
            target_ref_id=target_ref_id,
        )
        return Response(
            body=(f"added {checklist['name']}.{item_name} rev {row['rev']} (local)")
        )

    def _edit_retire_item(
        self, checklist: dict[str, Any], *, item: str | None
    ) -> Response:
        if item is None or not str(item).strip():
            raise BadInput(
                "edit(kind='checklist', op='retire_item') requires item=<name>"
            )
        item_name = str(item).strip()
        self._reject_if_shipped(checklist, item_name)
        removed = self.store.checklist_item_retire(
            checklist_id=checklist["id"], name=item_name
        )
        if not removed:
            raise NotFound(
                f"item {item_name!r} has no live rev on checklist {checklist['name']!r}"
            )
        return Response(body=f"retired {checklist['name']}.{item_name}")

    def _edit_verdict(
        self,
        checklist: dict[str, Any],
        *,
        target: str | int | None,
        item: str | None,
        verdict: str | None,
        evidence: dict[str, Any] | None,
        fingerprint: str | None,
        checked_by: str | None,
    ) -> Response:
        if item is None or not str(item).strip():
            raise BadInput("edit(kind='checklist', op='verdict') requires item=<name>")
        if verdict not in _VERDICTS:
            raise BadInput(
                f"edit(kind='checklist', op='verdict') verdict={verdict!r} "
                f"must be one of {list(_VERDICTS)}"
            )
        if evidence is not None and not isinstance(evidence, dict):
            raise BadInput("evidence= must be a dict")
        item_name = str(item).strip()
        ref = self._resolve_target(target)
        current = self.store.checklist_item_current(checklist["id"], item_name)
        if current is None:
            raise NotFound(
                f"item {item_name!r} has no live rev on checklist {checklist['name']!r}"
            )
        self.store.checklist_verdict_insert(
            target_ref_id=ref.id,
            checklist_id=checklist["id"],
            item_name=item_name,
            item_rev=current["rev"],
            verdict=verdict,
            evidence=evidence,
            fingerprint=(str(fingerprint).strip() if fingerprint else None),
            checked_by=checked_by,
        )
        return Response(
            body=(
                f"recorded {checklist['name']}.{item_name} = {verdict} for "
                f"{ref.kind}:{ref.label} (item rev {current['rev']})"
            )
        )

    def _edit_add_note(
        self,
        checklist: dict[str, Any],
        *,
        target: str | int | None,
        name: str | None,
        note_kind: str | None,
        body: str | None,
        item: str | None,
        re: str | None,
        about: Any,
        origin: str,
    ) -> Response:
        if name is None or not str(name).strip():
            raise BadInput(
                "edit(kind='checklist', op='add_note') requires name=<note-name>"
            )
        if note_kind not in NOTE_KINDS:
            raise BadInput(f"note_kind={note_kind!r} must be one of {list(NOTE_KINDS)}")
        if body is None or not str(body).strip():
            raise BadInput("edit(kind='checklist', op='add_note') requires body=")
        ref = self._resolve_target(target)
        item_name = str(item).strip() if item else None
        if item_name is not None:
            if self.store.checklist_item_current(checklist["id"], item_name) is None:
                raise NotFound(
                    f"item {item_name!r} has no live rev on checklist "
                    f"{checklist['name']!r}"
                )
        try:
            about_list = validate_about(about)
        except NoteError as exc:
            raise BadInput(str(exc)) from exc
        note_id = self.store.checklist_note_insert(
            target_ref_id=ref.id,
            checklist_id=checklist["id"],
            name=str(name).strip(),
            kind=note_kind,
            body=str(body).strip(),
            item_name=item_name,
            re=(str(re).strip() if re else None),
            about=about_list,
            origin=origin,
        )
        return Response(
            body=f"added {note_kind} {name!r} on {checklist['name']} (id={note_id})"
        )

    def _edit_remove_note(
        self,
        checklist: dict[str, Any],
        *,
        target: str | int | None,
        name: str | None,
    ) -> Response:
        if name is None or not str(name).strip():
            raise BadInput(
                "edit(kind='checklist', op='remove_note') requires name=<note-name>"
            )
        ref = self._resolve_target(target)
        removed = self.store.checklist_note_remove(
            target_ref_id=ref.id, checklist_id=checklist["id"], name=str(name).strip()
        )
        if not removed:
            raise NotFound(f"no live note {name!r} on {checklist['name']!r}")
        return Response(body=f"removed note {name!r} from {checklist['name']}")

    def _edit_assign(
        self, checklist: dict[str, Any], *, target: str | int | None
    ) -> Response:
        ref = self._resolve_target(target)
        created = self.store.checklist_assign(
            target_ref_id=ref.id, checklist_id=checklist["id"]
        )
        verb = "assigned" if created else "already assigned"
        return Response(body=f"{verb} {checklist['name']} to {ref.kind}:{ref.label}")

    def _edit_unassign(
        self, checklist: dict[str, Any], *, target: str | int | None
    ) -> Response:
        ref = self._resolve_target(target)
        removed = self.store.checklist_unassign(
            target_ref_id=ref.id, checklist_id=checklist["id"]
        )
        if not removed:
            raise NotFound(
                f"{checklist['name']!r} is not assigned to {ref.kind}:{ref.label}"
            )
        return Response(
            body=f"unassigned {checklist['name']} from {ref.kind}:{ref.label}"
        )

    # ── get ──────────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        target: str | int | None = None,
        fingerprint: str | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        v = (view or "").strip().lower()
        if v and v != "page":
            raise BadInput(f"unknown checklist view {view!r}", next="omit view=")
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        name = str(id).strip()
        checklist = self.store.checklist_get(name)
        if checklist is None:
            raise NotFound(f"checklist {name!r} not found")
        if target is None:
            return self._render_definition(checklist)
        ref = self._resolve_target(target)
        return self._render_status(checklist, ref, fingerprint=fingerprint)

    def _render_list(self) -> Response:
        rows = self.store.checklist_list(limit=50)
        if not rows:
            return Response(
                body="no checklists yet\n\nNext: put(kind='checklist', "
                "id='my-checklist', items=[{'name': 'item-one', "
                "'prevents': 'what breaks if this is skipped'}])"
            )
        table = [{"checklist": r["name"], "origin": r["origin"]} for r in rows]
        return Response(
            body=f"# {len(rows)} checklist(s)\n"
            + render_agent_table(table, schema=["checklist", "origin"])
        )

    def _render_definition(self, checklist: dict[str, Any]) -> Response:
        items = self.store.checklist_items_current(checklist["id"])
        head = f"# checklist {checklist['name']} ({checklist['origin']})"
        if not items:
            return Response(
                body=f"{head}\n\n(no items yet)\n\nNext: edit(kind='checklist', "
                f"id={checklist['name']!r}, op='add_item', item=..., "
                "prevents=...)"
            )
        table = [
            {
                "phase": it["phase"] or "—",
                "item": it["name"],
                "rev": it["rev"],
                "severity": it["severity"],
                "decidability": it["decidability"],
                "origin": it["origin"],
                "prevents": (it["prevents"] or "—")[:60],
            }
            for it in items
        ]
        return Response(
            body=f"{head}\n"
            + render_agent_table(
                table,
                schema=[
                    "phase",
                    "item",
                    "rev",
                    "severity",
                    "decidability",
                    "origin",
                    "prevents",
                ],
            )
        )

    def _render_status(
        self,
        checklist: dict[str, Any],
        ref: _Target,
        *,
        fingerprint: str | None,
    ) -> Response:
        head = f"# checklist {checklist['name']} — {ref.kind}:{ref.label}"
        if not self.store.checklist_assignment_live(
            target_ref_id=ref.id, checklist_id=checklist["id"]
        ):
            return Response(
                body=f"{head}\n\nno checklist assigned\n\nNext: "
                f"edit(kind='checklist', id={checklist['name']!r}, "
                f"op='assign', target={ref.kind!r} + ':' + "
                f"{ref.label!r})"
            )
        items = self.store.checklist_items_current(
            checklist["id"], target_ref_id=ref.id
        )
        if not items:
            return Response(body=f"{head}\n\nassigned — zero items defined")
        verdicts = self.store.checklist_verdicts_latest_for_target(
            target_ref_id=ref.id, checklist_id=checklist["id"]
        )
        table = []
        for it in items:
            v = verdicts.get(it["name"])
            status = _status_for(it, v, fingerprint)
            table.append(
                {
                    "phase": it["phase"] or "—",
                    "item": it["name"],
                    "severity": it["severity"],
                    "status": status,
                    "checked_at": str(v["checked_at"]) if v else "—",
                }
            )
        return Response(
            body=f"{head}\n"
            + render_agent_table(
                table, schema=["phase", "item", "severity", "status", "checked_at"]
            )
        )

    # ── search ───────────────────────────────────────────────────────────

    def search(
        self,
        *,
        q: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='checklist') needs q=",
                next="search(kind='checklist', q='tapeout')",
            )
        rows = self.store.checklist_list(q=str(q).strip(), limit=page_size)
        if not rows:
            return Response(body=f"no checklist matches for {q!r}")
        table = [{"checklist": r["name"], "origin": r["origin"]} for r in rows]
        return Response(
            body=f"# {len(rows)} checklist match(es) for {q!r}\n"
            + render_agent_table(table, schema=["checklist", "origin"])
        )

    # ── target resolution ────────────────────────────────────────────────

    def _resolve_target(self, target: str | int | None) -> _Target:
        """Resolve ``target=`` (``'<kind>:<id>'`` or a bare ref id) to a
        uniform handle — checklist targets are generic (pcb, cad, se, ...),
        unlike ``rxn``'s closed ``_SOURCE_KINDS`` list."""
        if target is None or not str(target).strip():
            raise BadInput(
                "target=<kind>:<id> (or a bare ref id) is required",
                next="target='pcb:my-board'",
            )
        t = str(target).strip()
        if ":" in t:
            kind, _, ident = t.partition(":")
            kind = kind.strip()
            ident = ident.strip()
            ref = self.store.get_ref(kind=kind, id=ident)
            if ref is None:
                try:
                    ref = self.store.get_ref(kind=kind, id=int(ident))
                except ValueError:
                    ref = None
            if ref is None:
                raise NotFound(f"target {t!r} not found")
            return _Target(id=ref.id, kind=ref.kind, label=ref.slug or str(ref.id))
        try:
            ref_id = int(t)
        except ValueError as exc:
            raise BadInput(
                f"target={target!r} must be '<kind>:<id>' or a bare ref id"
            ) from exc
        resolved = self.store.checklist_resolve_ref_any_kind(ref_id)
        if resolved is None:
            raise NotFound(f"target ref {ref_id} not found")
        rid, kind, _title = resolved
        return _Target(id=rid, kind=kind, label=str(rid))


def _status_for(
    item: dict[str, Any], verdict: dict[str, Any] | None, fingerprint: str | None
) -> str:
    """Three-valued status honesty: not checked / stale (with reason) /
    the recorded verdict. Never "clean" by omission."""
    if verdict is None:
        return "not checked"
    if verdict["item_rev"] < item["rev"]:
        return "stale (item revised)"
    if (
        fingerprint is not None
        and verdict.get("fingerprint") is not None
        and verdict["fingerprint"] != fingerprint
    ):
        return "stale (target changed)"
    return str(verdict["verdict"])


def _validate_item_payload(raw: Any) -> dict[str, Any]:
    """Vet one ``put(items=[...])`` element. Mandatory ``prevents`` is the
    cargo-cult filter (design doc: "no failure statement, no item")."""
    if not isinstance(raw, dict):
        raise BadInput("put(kind='checklist') items= entries must be dicts")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise BadInput("put(kind='checklist') each item requires name=")
    prevents = raw.get("prevents")
    if not prevents or not str(prevents).strip():
        raise BadInput(
            f"item {name!r} requires prevents= (the failure this item "
            "catches) — no failure statement, no item",
            next=(
                "put(kind='checklist', id=..., items=[{'name': "
                f"{name!r}, 'prevents': 'what breaks if this is skipped'}}])"
            ),
        )
    severity = raw.get("severity") or "advisory"
    if severity not in _SEVERITIES:
        raise BadInput(f"severity={severity!r} must be one of {list(_SEVERITIES)}")
    decidability = raw.get("decidability") or "judgment"
    if decidability not in _DECIDABILITY:
        raise BadInput(
            f"decidability={decidability!r} must be one of {list(_DECIDABILITY)}"
        )
    return {
        "name": name,
        "phase": (str(raw["phase"]).strip() if raw.get("phase") else None),
        "severity": severity,
        "decidability": decidability,
        "prevents": str(prevents).strip(),
        "applies": (str(raw["applies"]).strip() if raw.get("applies") else None),
        "body": (str(raw["body"]).strip() if raw.get("body") else None),
    }
