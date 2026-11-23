"""Evaluator registry for the auto-check worker (Slice 1b).

Each evaluator is a small callable that takes the store + the
``meta.auto_check`` dict and returns ``True`` / ``False`` /
``None`` (= not yet, leave the leaf open). The registry is a flat
``dict[str, Evaluator]`` keyed by the ``type`` field on the spec.

Add an evaluator by writing its module and registering it below.

Validation
==========

``validate_auto_check_spec`` is called from the handler boundary
(``TodoHandler.put``) so a typo in the ``type`` field surfaces
*at write time* with the full list of registered types in the
error. Evaluators can opt into write-time argument checks through
``VALIDATORS``. ``STORE_VALIDATORS`` adds checks that need live data
when the caller supplies a store. Evaluators also validate their inputs
when polled so existing specs still get checked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from precis.errors import BadInput
from precis.workers.auto_check_evaluators import (
    all_child_findings_resolved,
    checklist_clean,
    child_job_succeeded,
    derived_job_succeeded,
    discord_reply_received,
    netlist_drc_clean,
    paper_ingested,
    placement_legal,
    ref_done,
    route_complete,
    tag_present,
    time_past,
)

if TYPE_CHECKING:
    from precis.store import Store


class Evaluator(Protocol):
    """Evaluator surface — one callable per ``type``.

    Contract: raise ``BadInput`` ONLY for a spec-shape problem the
    caller can fix (the runner parks such a row once as
    ``auto-timeout``/``spec-error``); a store or backend failure must
    surface as any other exception so the row is retried next pass.
    """

    def __call__(
        self, store: Store, spec: dict[str, Any], *, ref_id: int
    ) -> bool | None: ...


#: Registry of ``type`` → evaluator callable. Add new evaluators
#: here; the validator + the worker both read this single dict.
REGISTRY: dict[str, Evaluator] = {
    "paper_ingested": paper_ingested.evaluate,
    "discord_reply_received": discord_reply_received.evaluate,
    "time_past": time_past.evaluate,
    "tag_present": tag_present.evaluate,
    "ref_done": ref_done.evaluate,
    "child_job_succeeded": child_job_succeeded.evaluate,
    "derived_job_succeeded": derived_job_succeeded.evaluate,
    "all_child_findings_resolved": all_child_findings_resolved.evaluate,
    # pcb-guided-place-route Slice 10 (retired spec — git history) — the
    # phase-machine gates.
    "placement_legal": placement_legal.evaluate,
    "route_complete": route_complete.evaluate,
    # pcb-guided-place-route Slice 8 (retired spec — git history) —
    # reads the latest persisted geometric-DRC run (precis.pcb.drc),
    # never recomputes it.
    "netlist_drc_clean": netlist_drc_clean.evaluate,
    # checklist-kind slice 2 (me693927) — every
    # blocking item of a checklist settled on the target, no fail.
    "checklist_clean": checklist_clean.evaluate,
}

# Optional write-time argument validators, keyed like REGISTRY. An
# evaluator earns an entry by exposing ``validate(spec) -> None`` that
# raises BadInput; it is called from validate_auto_check_spec after the
# shared type / on_resolve / timeout_at checks.
VALIDATORS: dict[str, Callable[[dict[str, Any]], None]] = {
    "paper_ingested": paper_ingested.validate,
    "checklist_clean": checklist_clean.validate,
    "ref_done": ref_done.validate,
}

STORE_VALIDATORS: dict[str, Callable[[Store, dict[str, Any]], None]] = {
    "ref_done": ref_done.validate_with_store,
}


def validate_auto_check_spec(spec: Any, *, store: Store | None = None) -> None:
    """Reject obviously-malformed ``meta.auto_check`` blocks at write time.

    Confirms:

    * ``spec`` is a dict.
    * ``spec['type']`` is a string in :data:`REGISTRY`.
    * ``spec['timeout_at']``, if present, is an ISO-shaped string
      (parseable by :func:`datetime.datetime.fromisoformat`).

    Per-evaluator argument validation is opt-in: an evaluator module
    that exposes ``validate(spec)`` is called here too (see
    :data:`VALIDATORS`), so a spec that can never evaluate is refused
    at the handler boundary instead of raising on every runner pass.
    Evaluators without one keep validating lazily at dispatch.
    A supplied store enables additional checks such as resolving record
    handles (see :data:`STORE_VALIDATORS`).
    """
    if not isinstance(spec, dict):
        raise BadInput(
            f"meta.auto_check must be a dict, got {type(spec).__name__}",
            next=("meta={'auto_check': {'type': '<evaluator>', ...evaluator-args}}"),
        )
    type_name = spec.get("type")
    if not isinstance(type_name, str) or type_name not in REGISTRY:
        raise BadInput(
            f"meta.auto_check.type {type_name!r} is not a registered evaluator",
            options=sorted(REGISTRY.keys()),
            next=(
                "see precis-auto-todo-help for the evaluator catalogue; "
                f"registered types: {sorted(REGISTRY.keys())}"
            ),
        )
    on_resolve = spec.get("on_resolve")
    if on_resolve is not None and on_resolve not in ("done", "open"):
        raise BadInput(
            f"meta.auto_check.on_resolve {on_resolve!r} is not valid",
            options=["done", "open"],
            next=(
                "'done' (default) completes the leaf on resolve; 'open' "
                "WAKES it instead — STATUS:open, waiting-for:* park tags "
                "dropped, the spec consumed (the snooze shape)"
            ),
        )
    timeout_at = spec.get("timeout_at")
    if timeout_at is not None:
        from datetime import datetime

        if not isinstance(timeout_at, str):
            raise BadInput(
                "meta.auto_check.timeout_at must be an ISO-shaped string "
                f"(got {type(timeout_at).__name__})",
                next="timeout_at='2026-07-01T12:00:00+00:00'",
            )
        try:
            datetime.fromisoformat(timeout_at)
        except ValueError as exc:
            raise BadInput(
                f"meta.auto_check.timeout_at is not parseable: {exc}",
                next="timeout_at='YYYY-MM-DDTHH:MM:SS+00:00' (ISO 8601)",
            ) from exc
    validator = VALIDATORS.get(type_name)
    if validator is not None:
        validator(spec)
    store_validator = STORE_VALIDATORS.get(type_name)
    if store is not None and store_validator is not None:
        store_validator(store, spec)


__all__ = ["REGISTRY", "VALIDATORS", "Evaluator", "validate_auto_check_spec"]
