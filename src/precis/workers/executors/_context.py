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

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Parses the job id out of ``JobHandler.put``'s ack body. Both ack
#: shapes carry it: ``created job id=N (STATUS:queued, …)`` and the
#: idem-dedupe ``existing job id=N for idem_key=…``.
_JOB_ID_IN_ACK = re.compile(r"\bjob id=(\d+)\b")


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

    def spawn_child(
        self,
        job_type: str,
        params: dict[str, Any],
        *,
        model: str | None = None,
        executor: str = "claude_inproc",
        idem_key: str | None = None,
    ) -> int:
        """Mint a child ``kind='job'`` parented on this job.

        The coordinator fan-out primitive (good-search design §Gaps 1):
        a campaign slice spawns triage / verify children under itself
        (``parent_id = self.ref_id``, riding the ADR 0044 job-parent
        extension — the parent must therefore be a *coordinator* job or
        ``JobHandler.put`` rejects) and later observes their terminal
        status on resume. Routed through :meth:`JobHandler.put` so
        submit-time validation (job_type registry, executor
        compatibility, params schema) and ``idem_key`` dedupe stay in
        one place. Deliberately injects **no** ``auto_check`` onto
        anything and requires no link — the coordinator reads child
        status itself; children must not auto-close or bubble onto
        anybody's todo.

        ``model`` folds into ``params['model']`` (per-child model rides
        in params; ``put(model=…)`` is the retry-only surface). Returns
        the child job's ref id — the in-flight job's id when
        ``idem_key`` deduped onto an existing non-terminal submit.
        """
        # Local imports: the handler layer imports the executors
        # package for the registry constants, so a module-level
        # import here would be a cycle.
        from precis.dispatch import Hub
        from precis.handlers.job import JobHandler

        merged = dict(params)
        if model is not None:
            merged["model"] = model
        handler = JobHandler(hub=Hub(store=self.store))
        resp = handler.put(
            job_type=job_type,
            executor=executor,
            params=merged,
            parent_id=self.ref_id,
            idem_key=idem_key,
        )
        m = _JOB_ID_IN_ACK.search(resp.body)
        if m is None:  # pragma: no cover — put()'s ack shape changed
            raise RuntimeError(
                f"spawn_child: could not parse job id from put ack: {resp.body!r}"
            )
        return int(m.group(1))
