"""Materialize the current editable canvas PNG on its source chunk."""

from __future__ import annotations

from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import JobTypeSpec


def _dispatch(ctx: DispatchContext, _spec: JobTypeSpec) -> None:
    job = ctx.store.get_ref(kind="job", id=ctx.ref_id)
    if job is None or job.parent_id is None:
        ctx.record_failure("figure_render needs a figure parent")
        return
    asset = ctx.store.drafts.figure_png(job.parent_id)
    if asset is None:
        ctx.record_failure("figure_render needs a live, drawable SVG figure parent")
        return
    _, sha, width, height = asset
    ctx.set_meta(png={"sha256": sha, "width": width, "height": height})
    ctx.append_chunk("job_summary", f"PNG {width}×{height}, SHA-256 {sha}")


SPEC = JobTypeSpec(
    name="figure_render",
    params_schema={"type": "object", "additionalProperties": False},
    compatible_executors=frozenset({"job_inproc"}),
    default_executor="job_inproc",
    requires=frozenset(),
    description="Render an editable SVG figure to a persisted PNG.",
    dispatch=_dispatch,
)
