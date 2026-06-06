"""job_type registry — what kinds of work the `job` substrate runs.

Each job_type module exports:

- ``PARAMS_SCHEMA`` (jsonschema dict): validated at submit time.
- ``COMPATIBLE_EXECUTORS`` (frozenset[str]): which executors this
  type can run under. Dispatcher rejects the put if the requested
  executor isn't in the set.
- ``REQUIRES`` (frozenset[str]): what the executor host must
  provide. Dispatcher rejects if the executor's PROVIDES set
  doesn't cover this.
- ``DESCRIPTION`` (str): one-line summary; surfaced in
  ``precis-job-help``.
- ``run(...)``: the worker entry point invoked by the executor's
  runner once a row is claimed.

The runtime registry below imports each type and bundles its
metadata into a :class:`JobTypeSpec`. Adding a new job_type =
write the module + add it to ``REGISTRY`` here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobTypeSpec:
    """Per-job-type declaration. Matches the module exports."""

    name: str
    params_schema: dict[str, Any]
    compatible_executors: frozenset[str]
    requires: frozenset[str]
    description: str
    run: Callable[..., Any]
    #: Optional submit-time check. Signature ``(store, *,
    #: gripe_id, params) -> str | None``: return an error message
    #: when the submit can't actually be carried out, ``None``
    #: when OK. The MCP-side ``JobHandler.put`` surfaces non-None
    #: as ``BadInput`` so the LLM gets an immediate rejection
    #: rather than a queued zombie job. ``None`` here means "no
    #: extra validation beyond the static schema / executor /
    #: REQUIRES checks".
    validate_submit: Callable[..., str | None] | None = None


def _load_fix_gripe() -> JobTypeSpec:
    # Lazy import keeps the heavy git/subprocess dependencies out
    # of the MCP dispatch path until the worker actually needs them.
    from precis.workers.job_types import fix_gripe

    return JobTypeSpec(
        name="fix_gripe",
        params_schema=fix_gripe.PARAMS_SCHEMA,
        compatible_executors=fix_gripe.COMPATIBLE_EXECUTORS,
        requires=fix_gripe.REQUIRES,
        description=fix_gripe.DESCRIPTION,
        run=fix_gripe.run,
        validate_submit=fix_gripe.validate_submit,
    )


#: Name → spec. Populated lazily on first access so the import
#: graph stays cheap for the MCP server.
_REGISTRY: dict[str, JobTypeSpec] = {}


def get_job_type(name: str) -> JobTypeSpec | None:
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name == "fix_gripe":
        _REGISTRY["fix_gripe"] = _load_fix_gripe()
        return _REGISTRY["fix_gripe"]
    return None


def known_job_types() -> list[str]:
    """List of registered job_type names (for error messages)."""
    return ["fix_gripe"]


__all__ = ["JobTypeSpec", "get_job_type", "known_job_types"]
