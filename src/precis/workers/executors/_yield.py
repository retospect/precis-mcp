"""Coordinator-job return types: ``Done``, ``Yield``, ``WakeWhen``.

The ``coordinator`` executor (see :mod:`precis.workers.executors.coordinator`)
hosts job_types whose dispatcher returns one of these:

- :class:`Done` — terminal outcome. The executor writes the
  summary chunk, transitions the job to ``STATUS:succeeded`` /
  ``STATUS:failed``, and stops claiming.
- :class:`Yield` — non-terminal pause. The executor persists the
  job's state into ``meta.coordinator_state``, writes ``meta.wake_when``,
  sets a ``STATUS:waiting_<reason>`` value, and releases the
  worker slot. A separate :mod:`precis.workers.wake_runner` ref
  pass scans for jobs whose wake condition has fired and re-tags
  them ``STATUS:queued`` so the coordinator picks them up on the
  next pass.

This is precis-mcp's coordinator substrate; plugin job_types
(``precis-dft``'s ``dft_campaign`` is the first real consumer)
import these types so the executor and the plugin agree on the
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: ``WakeWhen`` kinds. Each name decides which ``STATUS:waiting_*``
#: closed-status the executor sets when persisting a Yield, and
#: which SQL the :mod:`wake_runner` uses to detect satisfaction.
WakeKind = Literal[
    "children_done",
    "at_time",
    "tag_cleared",
    "tag_added",
]


@dataclass(frozen=True, slots=True)
class WakeWhen:
    """When should the wake_runner re-queue this job?

    Payload shape per kind:

    - ``children_done``: ``{"child_job_ids": [int, ...]}`` — wake
      when every listed job is in a terminal STATUS (``succeeded``
      / ``failed`` / ``cancelled``).
    - ``at_time``: ``{"ts": <unix-seconds>}`` — wake at or after
      this wall-clock instant.
    - ``tag_cleared``: ``{"tag": "ask-user:<phase>:*"}`` — wake
      when no tag matches the pattern. Pattern is a literal string
      or a ``foo:*`` glob (suffix match). Default mapping uses
      this for human-approval pauses.
    - ``tag_added``: ``{"tag": "manual_kick"}`` — wake when the
      named tag (exact match) appears.

    The cancel-override case (``STATUS:cancel_requested`` set on
    a waiting job) lives outside this contract: the wake_runner
    re-queues such jobs unconditionally so the coordinator can
    observe the cancel on its next slice.
    """

    kind: WakeKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Done:
    """Terminal outcome from a coordinator's ``spec.run``.

    ``summary`` is the human-readable account written as a
    ``job_summary`` chunk. ``success`` decides between
    ``STATUS:succeeded`` and ``STATUS:failed``. ``summary_meta``
    is merged into ``refs.meta`` so the agent's ``get(kind='job')``
    sees final scalars (wall_seconds, cost, …) inline.
    """

    summary: str
    success: bool = True
    summary_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Yield:
    """Non-terminal pause from a coordinator's ``spec.run``.

    ``state`` is the dispatcher's checkpoint — whatever shape the
    plugin wants. Persisted into ``meta.coordinator_state`` and
    handed back on the next slice via
    ``ctx.meta['coordinator_state']`` (the executor passes the row's
    ``meta`` into the DispatchContext at claim time).

    ``wake_when`` declares the condition the wake_runner watches
    for to re-queue the job.
    """

    state: dict[str, Any]
    wake_when: WakeWhen


__all__ = ["Done", "WakeKind", "WakeWhen", "Yield"]
