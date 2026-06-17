"""``DispatchContext`` — what plugin job_types receive.

The executor (``claude_inproc._run_one`` today, ``coordinator``
later) builds one of these per claimed job and hands it to the
job_type's ``dispatch(ctx, spec)`` callable. The context wraps the
store handle and ref state plus a small set of helpers that
plugins use to:

- Set ``STATUS:`` tags (``ctx.set_status``).
- Append ``job_event`` / ``job_summary`` / arbitrary chunks
  (``ctx.append_chunk``).
- Record a terminal failure with a one-line reason
  (``ctx.record_failure``).
- Merge fields into ``refs.meta`` (``ctx.set_meta``).
- Re-check cooperative cancel mid-flight
  (``ctx.is_cancel_requested``).

These wrap the corresponding ``claude_inproc`` module-private
helpers via closures so the executor remains the only place that
actually knows how a chunk gets written. Plugin job_types depend
on this dataclass interface, not on ``claude_inproc`` internals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DispatchContext:
    """Per-job dispatch context passed to plugin ``dispatch(ctx, spec)``.

    Built by the executor immediately after the cancel-poll, before
    invoking the job_type's dispatcher. All callables are
    closures over the executor's store handle + ref_id so the
    plugin doesn't have to thread those itself.
    """

    #: The Store handle. Plugins occasionally need to issue
    #: side-band queries (e.g. find a linked ref by relation) that
    #: don't fit the helpers below.
    store: Any
    #: The claimed job's ref_id.
    ref_id: int
    #: ``refs.title`` at claim time.
    title: str
    #: ``refs.meta`` at claim time (mutable copies inside the
    #: dispatcher are fine; the canonical row is the DB).
    meta: dict[str, Any]

    #: Replace the current ``STATUS:`` tag with ``STATUS:<value>``.
    set_status: Callable[[str], None]
    #: Append a chunk: ``append_chunk(kind, text)``. Kind is
    #: typically ``'job_event'`` (forensics) or ``'job_summary'``
    #: (the agent-readable account).
    append_chunk: Callable[[str, str], None]
    #: Merge keyword fields into ``refs.meta``:
    #: ``set_meta(wall_seconds=42, branch='foo')``.
    set_meta: Callable[..., None]
    #: Terminal-failure recorder. Writes a ``job_event`` reason
    #: chunk, transitions to ``STATUS:failed``, and (Slice-5)
    #: bubbles the failure to the parent todo.
    record_failure: Callable[[str], None]
    #: Cooperative cancel check. Returns ``True`` when a
    #: ``STATUS:cancel_requested`` tag has been added since the
    #: job was claimed. Plugins running multi-phase work should
    #: poll this between phases.
    is_cancel_requested: Callable[[], bool]
