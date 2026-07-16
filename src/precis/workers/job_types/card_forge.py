"""``card_forge`` job_type — the morning card work, before the reading brief.

Third daily reading-loop job beside ``reading_brief`` (06:00) and ``meditation``
(21:00); scheduled at 05:30 so the day's cards exist before the brief composes
(its recall lane reports them). Deterministic in-process producer under
``claude_inproc`` (melchior — the authoring calls use a nice model via the
litellm proxy). Driven by a ``level:recurring`` todo
(``meta.schedule={'cron':'30 5 * * *'}``, ``meta.executor='claude_inproc'``,
``meta.job_type='card_forge'``). Calls
:func:`precis.reading.cards.run_card_forge`: mastery-from-Anki refresh → the
retire/teach-prereq/escalate/rewrite ladder over stale leech cards
(observe-first — report mode unless ``PRECIS_CARD_FORGE_AUTONOMY=act``) → mint
today's new cards from cardless concepts. New/rewritten cards ride the existing
``precis anki-sync`` to the phone.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cohort": {
            "type": ["string", "null"],
            "description": "Concept cohort to card (else corpus-wide, newest first).",
        },
        "per_day": {
            "type": "integer",
            "minimum": 0,
            "description": "Max concepts to card today (default 5).",
        },
        "min_age_days": {
            "type": "number",
            "minimum": 0,
            "description": "Days a card gets to prove itself before rework (default 4).",
        },
    },
    "additionalProperties": False,
}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job."""
    from precis.reading.cards import run_card_forge

    params = (ctx.meta or {}).get("params") or {}
    kwargs: dict[str, Any] = {}
    if params.get("cohort"):
        kwargs["cohort"] = str(params["cohort"])
    if params.get("per_day") is not None:
        kwargs["per_day"] = int(params["per_day"])
    if params.get("min_age_days") is not None:
        kwargs["min_age_days"] = float(params["min_age_days"])

    try:
        report = run_card_forge(ctx.store, **kwargs)
    except Exception as exc:
        log.warning("card_forge job: pass raised", exc_info=True)
        ctx.record_failure(f"card_forge: pass raised: {exc}")
        return

    lines = report.lines()
    summary = "\n".join(lines) if lines else "card_forge: nothing to do"
    ctx.append_chunk("job_summary", summary)
    ctx.set_meta(
        minted_concepts=len(report.minted),
        rework_decisions=len(report.decisions),
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("card_forge runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="card_forge",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),  # deterministic in-process — no executor capabilities
    description=(
        "Morning card work: refresh mastery, rework failing anki cards, mint "
        "today's new cards from concepts."
    ),
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
