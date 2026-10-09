"""``type='checklist_clean'`` — gate a todo on a target's checklist status.

Resolves ``True`` when every **blocking** item of the named checklist is
settled on the target with no ``fail`` — a settled item is a current
``pass``/``n/a``/``waived`` verdict, or a live tool checker reading
``pass``. ``False`` when any blocking item reads ``fail`` (ledger or live
checker). ``None`` (leave the leaf open) when a blocking item is still
``not checked``, stale, or its checker could not run — and when the
checklist is not assigned to the target at all: "not yet" is not
"clean", the same three-valued honesty as ``netlist_drc_clean``.
Advisory items never block. Status derivation is
:func:`precis.checklist.compute_statuses`, shared with the handler's
status view so a todo and an agent reading the view agree.

Spec
====

```json
{"type": "checklist_clean", "checklist": "pcb-tapeout", "target": "pcb:sensor-node"}
```

``target`` is ``'<kind>:<slug-or-id>'``; ``checklist`` a checklist name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.checklist import compute_statuses
from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store


def validate(spec: dict[str, Any]) -> None:
    name = spec.get("checklist")
    if not isinstance(name, str) or not name.strip():
        raise BadInput(
            "checklist_clean needs checklist=<name>",
            next="meta.auto_check.checklist='pcb-tapeout'",
        )
    target = spec.get("target")
    if not isinstance(target, str) or ":" not in target:
        raise BadInput(
            "checklist_clean needs target='<kind>:<slug-or-id>'",
            next="meta.auto_check.target='pcb:sensor-node'",
        )


def _resolve_target(store: Store, target: str) -> tuple[int, str] | None:
    kind, _, ident = target.partition(":")
    kind, ident = kind.strip(), ident.strip()
    ref = store.get_ref(kind=kind, id=ident)
    if ref is None and ident.isdigit():
        ref = store.get_ref(kind=kind, id=int(ident))
    return None if ref is None else (ref.id, ref.kind)


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    validate(spec)
    checklist = store.checklist_get(str(spec["checklist"]).strip())
    if checklist is None:
        raise BadInput(f"checklist {spec['checklist']!r} not found")
    resolved = _resolve_target(store, str(spec["target"]))
    if resolved is None:
        return None  # target not (yet) there — not yet, not a failure
    ref_id, kind = resolved
    assigned = store.checklist_assignment_live(
        target_ref_id=ref_id, checklist_id=checklist["id"]
    ) or kind in (checklist.get("default_for") or [])
    if not assigned:
        return None
    items = store.checklist_items_current(checklist["id"], target_ref_id=ref_id)
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=ref_id, checklist_id=checklist["id"]
    )
    statuses = compute_statuses(
        store, items=items, verdicts=verdicts, kind=kind, ref_id=ref_id
    )
    blocking = [st for st in statuses if st.blocking]
    if any(st.status == "fail" for st in blocking):
        return False
    if any(not st.settled for st in blocking):
        return None
    return True
