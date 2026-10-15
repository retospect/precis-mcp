"""``pcb_review`` job_type — the re-check pass over one pcb design
(docs/backlog/pcb-datasheet-review.md slice 7, in-scope 7).

Works the ``pcb-tapeout`` judgment items that are ``not checked`` or
``stale`` on the design, one fresh-context model call each, and writes a
``machine``-origin verdict anchored to the refdes/nets it names with the
decisive line quoted — or, when the evidence is missing (a part with no
datasheet facts), a ``not checkable (…)`` question note on the item's
thread, never a pass. Queue, evidence, prompt and write live in
:mod:`precis.pcb.review`; this module is the job shell.

A re-run on an unchanged design makes no model call: settled items never
re-enter the queue, and a ``not checkable`` note keyed on the target
fingerprint + input revisions skips its item until one of them moves.

The fresh check-surface run is NOT this job's: ``put(kind='pcb',
args={'op':'review'})`` runs ``view='check'`` (persisted) before it mints
the job, because that run lives in the handler layer workers may not
import. The evidence quotes the latest persisted run by id.

Runs under ``job_inproc`` (bounded — ``params.limit`` caps the items one
run works); the router call is in-process (MEDIUM tier,
:data:`precis.pcb.review.LLM_SOURCE`), so no ``requires``.
``put(kind='job', job_type='pcb_review', parent_id=<pcb ref>,
params={'pcb_ref_id': N})`` mints it directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.pcb import review
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pcb_ref_id": {"type": "integer"},
        # One MEDIUM-tier call per item; the checklist has ~40 judgment
        # items, so a run works a bounded slice and a re-run continues.
        "limit": {"type": "integer", "minimum": 1, "maximum": 60},
        # Re-check one named item only.
        "item": {"type": "string", "minLength": 1},
    },
    "required": ["pcb_ref_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Re-check a pcb design against pcb-tapeout: work the not-checked and "
    "stale judgment items (one fresh-context call each), write anchored "
    "machine verdicts quoting the decisive line, or a 'not checkable' note."
)

_DEFAULT_LIMIT = 15


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pcb_ref_id = int(params["pcb_ref_id"])
    limit = int(params.get("limit") or _DEFAULT_LIMIT)
    only = str(params.get("item") or "").strip() or None

    store = ctx.store
    ref = store.get_ref(kind="pcb", id=pcb_ref_id)
    if ref is None:
        ctx.record_failure(
            f"pcb_review: no pcb design ref_id={pcb_ref_id}", failure_class="infra"
        )
        return
    slug = str(ref.slug or pcb_ref_id)
    title = str(ref.title or slug)

    q = review.queue(store, pcb_ref_id, item=only)
    if q.checklist is None or q.reason is not None:
        ctx.record_failure(f"pcb_review: {slug}: {q.reason}", failure_class="infra")
        return
    checklist_id = int(q.checklist["id"])
    graph = store.pcb_graph(pcb_ref_id)
    revisions = review.input_revisions(store, graph)

    work, skipped = review.pending(store, pcb_ref_id, q, revisions)
    settled = sum(1 for st in q.statuses if st.settled)
    if not work:
        ctx.append_chunk(
            "job_summary",
            f"{slug}: nothing to review — {settled} item(s) settled, "
            f"{skipped} not checkable at the current inputs; no model call\n",
        )
        return
    work = work[:limit]

    evidence_text = review.evidence(store, pcb_ref_id, graph)
    lines: list[str] = []
    failures: list[str] = []
    cancelled = False
    for st, key in work:
        if ctx.is_cancel_requested():
            cancelled = True
            break
        if ctx.renew_lease is not None and not ctx.renew_lease():
            return
        try:
            answer = review.ask(
                title=title, st=st, evidence_text=evidence_text, ref_id=pcb_ref_id
            )
            lines.append(
                review.record(
                    store,
                    pcb_ref_id,
                    checklist_id=checklist_id,
                    st=st,
                    answer=answer,
                    revisions=revisions,
                    graph=graph,
                    nc_key=key,
                    job_ref_id=ctx.ref_id,
                )
            )
        except Exception as exc:
            # One bad answer (or write) never sinks the run: the item
            # stays in the queue (not checked / stale), the next run
            # retries it, and what was already written is reported.
            log.warning("pcb_review: %s on %s failed: %s", st.name, slug, exc)
            failures.append(f"{st.name}: {exc}")
            ctx.append_chunk("job_event", f"pcb_review: {st.name} failed: {exc!r}")

    if cancelled and not lines:
        ctx.append_chunk("job_summary", f"{slug}: cancelled before any item\n")
        return
    if not lines and failures:
        ctx.record_failure(
            f"pcb_review: every item failed on {slug} ({len(failures)}): "
            + "; ".join(failures[:3]),
            failure_class="infra",
        )
        return
    left = len(q.todo) - skipped - len(lines) - len(failures)
    tail = []
    if failures:
        tail.append(f"{len(failures)} failed (retried next run)")
    if left > 0:
        tail.append(f"{left} still queued (limit {limit}) — re-run to continue")
    ctx.append_chunk(
        "job_summary",
        f"{slug}: reviewed {len(lines)} item(s)"
        + (f"; {'; '.join(tail)}" if tail else "")
        + "\n"
        + "\n".join(f"- {line}" for line in lines)
        + f"\nNext: get(kind='checklist', id='{review.CHECKLIST}', "
        f"target='pcb:{slug}')\n",
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("pcb_review runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="pcb_review",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
