"""``doctor_tick`` job_type — one LLM judgment pass over the fleet's own
published health surfaces (self-healing spine Layer 3,
``docs/backlog/doctor-tick-report.md``, ``docs/backlog/self-healing-spine.md``
§Layer 3).

Layers 0-2 of the spine are deterministic, zero-LLM, and never reasoned
"across" their own signals — a broken pass and a noisy-but-working one look
identical to a fixed-threshold probe. The doctor is the judgment layer on
top: a recurring ``claude_inproc`` agent tick, minted by a host-agnostic
``scheduler.py`` cadence (:func:`precis.workers.scheduler._run_doctor_tick_mint`),
that reads what Layers 1-2 already publish, classifies by ratio (broken pass
vs noisy-but-working vs baseline noise), diagnoses via culprit-localization
walks, and acts ONLY by filing/annotating gripes — never by raising an alert
itself (machinery-only write path, decided 2026-08-12).

**Starting dial: ``report``.** Per
``docs/backlog/self-healing-spine.md``'s autonomy dial, this ships at the
report rung only: gather + classify + diagnose + author the daily report +
file/annotate gripes. ``heal``/``draft`` are a later slice.

**Tool posture.** Runs under the deep-reviewer's tier-1 deny list
(:data:`precis.workers.review._REVIEWER_DISALLOWED_TOOLS` — imported, never
forked: no ``Bash``/``Write``/``Edit``/``WebFetch``/``WebSearch`` and no
precis ``edit``/``delete``/``tag``/``link``) at the permissive tier — the
named prerequisite for a dedicated ``write:gripe`` envelope value
(gr179501) hasn't landed yet. ``put`` stays allowed: it is the doctor's one
sanctioned write, for filing/annotating gripes.

**Report authoring, mirrors the reviewer digest-write shape.** The doctor
does not write its own report body via a tool call (that would need
``edit``, which is denied). Like :func:`precis.workers.review.run_review_pass`
turns a reviewer's plain final-text reply into the tier-tagged digest memory,
:func:`run` here takes the agent's final reply and appends it as the day's
report body (:mod:`precis.workers.doctor_report` owns the per-UTC-day
``draft`` ref this lands on) — the model's own tool budget is spent gathering,
classifying, diagnosing, and filing gripes, not on writing its own artifact.

Same dispatch shape as ``plan_tick`` (a hardcoded ``run`` entry in
``claude_inproc._run_one``, not the plugin ``dispatch`` protocol): the
executor arm (``executors/claude_inproc.py::_run_doctor_tick``) appends the
job's own ``job_summary``/``job_result`` chunks and persists
``meta.transcript`` (capped, same idiom as plan_tick) on the JOB ref — a
separate artifact from the day's report draft this module writes to.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from precis.utils.llm.router import Tier
from precis.workers.review import _REVIEWER_DISALLOWED_TOOLS

log = logging.getLogger(__name__)


DESCRIPTION: str = (
    "one LLM doctor pass: gather published health surfaces, classify by "
    "ratio, diagnose, author the daily report, file/annotate gripes"
)

#: No caller-supplied knobs today — the tick is a fixed, parameterless
#: cadence fire (unlike plan_tick's per-tag model choice). Kept as an
#: explicit empty object (not omitted) so a future knob (e.g. a per-tick
#: model override) is a schema addition, not a shape change.
PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})

#: Same two host capabilities every ``claude_agent``-transport tick needs
#: (the binary + an MCP config so the agent can call back via precis
#: tools) — both are provided by the ``claude_inproc`` executor.
REQUIRES: frozenset[str] = frozenset({"claude_bin", "mcp_config"})

#: Local-first tier (spine constitution law 8) — the same tier the
#: structural/deep reviewers dispatch at, so the same
#: ``PRECIS_LLM_BACKEND``/``PRECIS_MODEL_*`` knobs retune it.
_TIER = Tier.BIG

#: A tick gathers from several surfaces, reasons across them, dedups
#: against open gripes, and authors a four-section report — a bit more
#: room than a single-purpose reviewer (structural: 30/900s), but the same
#: order of magnitude; this is a report pass, not a fix attempt.
_MAX_TURNS = 40
_TIMEOUT_S = 1200.0


@dataclass(frozen=True, slots=True)
class DoctorTickOutcome:
    """Result of one doctor tick, read by the executor's job bookkeeping.

    ``text``/``raw_text`` mirror ``plan_tick.PlanTickOutcome``'s
    ``stdout``/duration shape closely enough that the executor arm can
    reuse the same transcript-cap + job_summary/job_result idiom.
    ``report_ref_id`` is ``None`` on any failure (nothing was written).
    """

    exit_code: int
    text: str
    raw_text: str
    error: str | None
    duration_s: float
    cost_usd: float | None
    report_ref_id: int | None


#: Packaged doctor directive prompt — the SSOT, persona-neutral (the
#: agent's identity comes from ambient project config, same as every
#: other claude_inproc claude-agent tick; the doctor carries no separate
#: persona file the way ``dream_agent`` does).
_PACKAGED_PROMPT = "precis.data.prompts"
_PACKAGED_PROMPT_FILE = "doctor-prompt.md"


def _load_prompt() -> str | None:
    """The doctor directive prompt from the packaged data file, or
    ``None`` if the packaged resource is somehow unreadable (should not
    happen in practice — caught defensively, same as ``dream_agent``'s
    loader, since a broken resource must fail the tick cleanly rather
    than crash the worker)."""
    try:
        from importlib import resources

        return (
            resources.files(_PACKAGED_PROMPT)
            .joinpath(_PACKAGED_PROMPT_FILE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        log.exception("doctor_tick: packaged doctor prompt unreadable")
        return None


def _compose_prompt(base_prompt: str, *, date_tag: str) -> str:
    """Prepend the tick's one piece of variable context — today's UTC
    date — to the packaged directive. Everything else the doctor needs
    (which surfaces to read, the gripe-dedup discipline, the untrusted-
    input framing) lives in the packaged prompt so it stays reviewable
    as prose, not scattered across f-strings."""
    return f"Today's UTC date is {date_tag}.\n\n{base_prompt}"


def run(
    *,
    # tests call this directly with a bare object() sentinel (the actual
    # store use is hidden behind the monkeypatched dispatch + doctor_report
    # collaborators), diverging from Store.
    store: Any,
    job_ref_id: int,
    params: dict[str, Any],
    **_kw: Any,
) -> DoctorTickOutcome:
    """Run one doctor tick and, on a non-empty reply, append it as today's
    report body. Returns a :class:`DoctorTickOutcome` for the executor's
    own job-ref bookkeeping (job_summary/job_result/transcript) —
    see the module docstring for the split between the job ref (this
    tick's own audit trail) and the report ref (the day's artifact).
    """
    del params  # no knobs today; kept for the uniform run() signature
    # Lazy import (mirrors plan_tick's ``_run_claude_tick``): resolved at
    # call time so tests can monkeypatch ``router.route`` directly,
    # rather than binding a module-level reference doctor_tick would
    # never see updated.
    from precis.utils.llm.router import LlmRequest, route
    from precis.workers import doctor_report

    started = time.monotonic()
    base_prompt = _load_prompt()
    if base_prompt is None:
        return DoctorTickOutcome(
            exit_code=1,
            text="",
            raw_text="",
            error="doctor_tick: packaged doctor prompt unreadable",
            duration_s=time.monotonic() - started,
            cost_usd=None,
            report_ref_id=None,
        )

    date_tag = doctor_report.utc_date_tag()
    prompt = _compose_prompt(base_prompt, date_tag=date_tag)

    mcp_config = os.environ.get("PRECIS_MCP_CONFIG", "")
    if not mcp_config:
        log.warning(
            "doctor_tick: PRECIS_MCP_CONFIG unset; the doctor can't call back "
            "via MCP — gather/dedup/gripe-filing won't land"
        )

    res = route(
        LlmRequest(
            tier=_TIER,
            source="doctor_tick",
            prompt=prompt,
            tools_needed=True,
            mcp_config=os.path.abspath(mcp_config) if mcp_config else None,
            max_turns=_MAX_TURNS,
            timeout_s=_TIMEOUT_S,
            # Tier-1 deny (gr179501, imported from review.py — never
            # forked): read + gather + the gripe carve-out only; no
            # mutate/fs-write/shell/web. ``put`` stays allowed.
            disallowed_tools=_REVIEWER_DISALLOWED_TOOLS,
            # Stream-json for the executor's transcript capture, same as
            # every other claude-agent tick (plan_tick, the reviewers).
            output_format="stream-json",
            extra_args=("--verbose",),
            ref_id=job_ref_id,
        )
    )
    duration = (
        res.duration_s if res.duration_s is not None else time.monotonic() - started
    )

    if res.error:
        return DoctorTickOutcome(
            exit_code=1,
            text="",
            raw_text=res.raw_text or "",
            error=res.error,
            duration_s=duration,
            cost_usd=res.cost_usd,
            report_ref_id=None,
        )

    text = (res.text or "").strip()
    if not text:
        return DoctorTickOutcome(
            exit_code=1,
            text="",
            raw_text=res.raw_text or "",
            error="doctor_tick: empty reply — nothing to report",
            duration_s=duration,
            cost_usd=res.cost_usd,
            report_ref_id=None,
        )

    ref, _created = doctor_report.find_or_create_report(store, date_tag)
    # Same-day re-ticks (the 8h cadence fires up to 3x within one UTC
    # day, per the freshness-window margin) APPEND rather than replace —
    # the day's report is a running log of this UTC day's ticks, and
    # append avoids the retire/cascade edge cases a wholesale
    # replace would need on a single-section draft.
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text=text, split=True
    )

    return DoctorTickOutcome(
        exit_code=0,
        text=text,
        raw_text=res.raw_text or "",
        error=None,
        duration_s=duration,
        cost_usd=res.cost_usd,
        report_ref_id=int(ref.id),
    )


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "DoctorTickOutcome",
    "run",
]
