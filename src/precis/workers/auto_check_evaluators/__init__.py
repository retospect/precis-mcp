"""Evaluator registry for the auto-check worker (Slice 1b).

Each evaluator is a small callable that takes the store + the
``meta.auto_check`` dict and returns ``True`` / ``False`` /
``None`` (= not yet, leave the leaf open). The registry is a flat
``dict[str, Evaluator]`` keyed by the ``type`` field on the spec.

Adding a new evaluator is two lines: write the module, register it
below. No protocol class, no decorators — keep the surface tiny so
asa-worker can read this file in one glance.

Validation
==========

``validate_auto_check_spec`` is called from the handler boundary
(``TodoHandler.put``) so a typo in the ``type`` field surfaces
*at write time* with the full list of registered types in the
error. Schema specifics (the doi / ask_message_id / at fields)
are validated by the evaluator itself when it runs — we don't
re-implement that surface here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from precis.errors import BadInput
from precis.workers.auto_check_evaluators import (
    all_child_findings_resolved,
    child_job_succeeded,
    derived_job_succeeded,
    discord_reply_received,
    paper_ingested,
    tag_present,
    time_past,
)

if TYPE_CHECKING:
    from precis.store import Store


class Evaluator(Protocol):
    """Evaluator surface — one callable per ``type``."""

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
    "child_job_succeeded": child_job_succeeded.evaluate,
    "derived_job_succeeded": derived_job_succeeded.evaluate,
    "all_child_findings_resolved": all_child_findings_resolved.evaluate,
}


def validate_auto_check_spec(spec: Any) -> None:
    """Reject obviously-malformed ``meta.auto_check`` blocks at write time.

    Confirms:

    * ``spec`` is a dict.
    * ``spec['type']`` is a string in :data:`REGISTRY`.
    * ``spec['timeout_at']``, if present, is an ISO-shaped string
      (parseable by :func:`datetime.datetime.fromisoformat`).

    Per-evaluator argument validation runs lazily at evaluator
    dispatch — this keeps the registry's surface tight and means
    a future evaluator with a complicated arg shape doesn't have
    to also register a validator.
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
                "see precis-auto-tasks-help for the evaluator catalogue; "
                f"registered types: {sorted(REGISTRY.keys())}"
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


__all__ = ["REGISTRY", "Evaluator", "validate_auto_check_spec"]
