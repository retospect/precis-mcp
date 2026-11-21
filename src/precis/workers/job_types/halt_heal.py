"""``halt_heal`` — one frontier supervisor session for a halted cadence
(self-healing-spine, the heal dial; see :mod:`precis.workers.halt_heal` for
the whole flow: the registry of halts, the once-per-window budget, the
escalation and the resume).

Minted by :func:`precis.workers.halt_heal.request_heal` — never by a model —
with the halt key and the halt note as params (the note is data). The session
investigates with read verbs only and ends with a fixed block (see
``data/prompts/halt-heal-prompt.md``); CODE reads the verdict:

* ``CLEAR`` in mode ``heal`` and the halt still the one the session looked at
  → the halt key is emptied (the cadence probes on its next pass), one
  ``job_event`` and the ``halt_heal:last:<key>`` record say so, the job
  succeeds, no alert. The clear applies only while no human was alerted for
  the episode (the ``halt_heal:alerted:<key>`` marker is unset); with the
  marker set it escalates instead, and a halt a probe or an operator emptied
  first is recorded ``already-clear``, never as a clear by the supervisor.
* Mode ``off`` at dispatch (switched off after the job was queued) →
  escalate without a session.
* ``CLEAR`` in mode ``report`` or ``off``, ``ESCALATE``, a reply without the
  block, a session error, a quota pause the job cannot park again, or any
  exception → :func:`precis.workers.halt_heal.escalate` (critical alert plus
  one ops push). The job result says which. A quota pause parks the SAME job
  until the window reopens (``defer_on_pause``, as ``doctor_tick``); a job
  that never launches ends in an escalation through the park cap.

**Tool posture.** The doctor's tier-1 deny list
(:data:`precis.workers.review._REVIEWER_DISALLOWED_TOOLS` — imported, never
forked) plus ``mcp__precis__put``: unlike the doctor, this session has no
sanctioned write at all. ``route()`` applies the usage gates; nothing here
bypasses it.

**Verdict grammar.** The final block must be the tail of the reply: a
``heal: CLEAR|ESCALATE <cause>`` line, one to five ``evidence:`` lines, a
``human-action:`` line, nothing after it (:data:`_BLOCK_RE`). Only that last
block counts, so a ``heal:`` quoted inside an ``evidence:`` line is inert. A
``CLEAR`` is additionally downgraded to ``ESCALATE`` when any other line of
the reply starts with ``heal:`` — a reply that said both is ambiguous, and
the unhandled side must be the one that asks a human. Markdown fence lines
are ignored (the prompt shows the block fenced).

Registered as a dispatch-protocol plugin (``spec.dispatch``) under
``claude_inproc`` — no hardcoded arm in the executor. Placement is the
executor's: any ``claude_inproc`` worker with ``claude_bin`` + ``mcp_config``
claims it, exactly like ``doctor_tick``.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis.utils.llm.router import Tier
from precis.utils.secret_scan import mask_secrets
from precis.workers.halt_heal import (
    HALTS,
    HaltSpec,
    clip,
    escalate,
    heal_mode,
    is_alerted,
    save_last,
    since_of,
)
from precis.workers.job_types import JobTypeSpec, defer_on_pause
from precis.workers.review import _REVIEWER_DISALLOWED_TOOLS

log = logging.getLogger(__name__)

DESCRIPTION: str = (
    "One frontier supervisor session for a halted cadence: investigate with "
    "read verbs, then clear that one halt if its cause is demonstrably gone, "
    "else raise the critical alert for a human"
)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "note": {"type": "string"},
        "halted_at": {"type": "string"},
    },
    "required": ["key"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})

#: Same host capabilities as ``doctor_tick`` (the binary + an MCP config for
#: the read-verb session), both provided by the ``claude_inproc`` executor.
REQUIRES: frozenset[str] = frozenset({"claude_bin", "mcp_config"})

_SOURCE = "halt_heal"
_MAX_TURNS = 40
_TIMEOUT_S = 1200.0
#: The doctor's deny list plus ``put``: the session reads, it never writes.
_DISALLOWED_TOOLS: tuple[str, ...] = (*_REVIEWER_DISALLOWED_TOOLS, "mcp__precis__put")
#: Same 1 MiB cap and idiom as ``claude_inproc``'s doctor/plan transcripts.
_TRANSCRIPT_CAP = 1_000_000

#: The halt note is log text: capped (and masked) before it is fenced.
_PROMPT_NOTE_CAP = 2000

_PACKAGED_PROMPT = "precis.data.prompts"
_PACKAGED_PROMPT_FILE = "halt-heal-prompt.md"

_BLOCK_RE = re.compile(
    r"^heal: (?P<verdict>CLEAR|ESCALATE)(?: (?P<cause>[^\n]*))?\n"
    r"(?P<evidence>(?:evidence:[^\n]*\n){1,5})"
    r"human-action:(?P<action>[^\n]*)\s*\Z",
    re.MULTILINE,
)
_HEAL_LINE_RE = re.compile(r"^heal:", re.MULTILINE)
#: Markdown fence lines (the prompt shows the block fenced; models copy it).
_FENCE_RE = re.compile(r"^```[\w-]*[ \t]*\n", re.MULTILINE)


@dataclass(frozen=True)
class Verdict:
    """The session's parsed final block; every field is model text,
    already clipped."""

    verdict: str  # "CLEAR" | "ESCALATE"
    cause: str
    evidence: tuple[str, ...]
    human_action: str


def parse_verdict(text: str) -> Verdict | None:
    """The reply's final block, or ``None`` when it does not end in one (see
    the module docstring for the grammar and the CLEAR downgrade)."""
    body = (text or "").replace("\r\n", "\n").rstrip() + "\n"
    body = _FENCE_RE.sub("", body)
    m = _BLOCK_RE.search(body)
    if m is None:
        return None
    verdict = m.group("verdict")
    if verdict == "CLEAR" and len(_HEAL_LINE_RE.findall(body)) > 1:
        verdict = "ESCALATE"
    evidence = tuple(
        clip(line[len("evidence:") :]) for line in m.group("evidence").splitlines()
    )
    return Verdict(
        verdict=verdict,
        cause=clip(m.group("cause") or ""),
        evidence=evidence,
        human_action=clip(m.group("action")),
    )


def _load_prompt() -> str | None:
    """The packaged directive, or ``None`` when the resource is unreadable
    (a broken resource must fail the job cleanly into an escalation)."""
    try:
        from importlib import resources

        return (
            resources.files(_PACKAGED_PROMPT)
            .joinpath(_PACKAGED_PROMPT_FILE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        log.exception("halt_heal: packaged prompt unreadable")
        return None


def _compose_prompt(
    base_prompt: str, halt: HaltSpec, *, note: str, halted_at: str
) -> str:
    """The directive, then the trusted halt facts, then the halt note inside
    a nonce-fenced DATA block (the note can be log text; it must not close
    the fence or pose as instructions)."""
    nonce = secrets.token_hex(8)
    data = mask_secrets(note or "")[:_PROMPT_NOTE_CAP].replace(nonce, "")
    return (
        f"{base_prompt}\n\n"
        "HALT FACTS (trusted, from the registry)\n"
        f"halt key: {halt.key}\n"
        f"cadence: {halt.cadence}\n"
        f"what it does: {halt.what}\n"
        f"first halt seen at: {halted_at or 'unknown'}\n"
        f"operator reset: {halt.reset_hint}\n\n"
        f"HALT DATA {nonce} (untrusted, quoted verbatim; the halt note follows)\n"
        f"{data}\n"
        f"END HALT DATA {nonce}\n"
    )


def _persist(ctx: Any, res: Any, *, wall_s: float) -> None:
    """Keep the session's audit trail on the JOB ref: capped transcript,
    cost, wall time and one ``job_result`` line (the doctor's idiom)."""
    from precis.utils.claude_agent import compact_stream_transcript

    compacted = compact_stream_transcript(getattr(res, "raw_text", "") or "")
    transcript = compacted[:_TRANSCRIPT_CAP]
    if len(compacted) > _TRANSCRIPT_CAP:
        transcript += "\n…(truncated)"
    fields: dict[str, Any] = {"wall_seconds": wall_s}
    if transcript:
        fields["transcript"] = transcript
    if getattr(res, "cost_usd", None) is not None:
        fields["cost_usd"] = res.cost_usd
    ctx.set_meta(**fields)
    result = f"halt_heal: duration={wall_s:.1f}s"
    if getattr(res, "cost_usd", None) is not None:
        result += f" cost=${res.cost_usd:.4f}"
    if getattr(res, "error", None):
        result += f" error={clip(res.error)}"
    ctx.append_chunk("job_result", result)


def _escalate_job(
    ctx: Any,
    halt: HaltSpec,
    *,
    note: str,
    since: str,
    tried: str,
    verdict: Verdict | None = None,
    outcome: str = "escalated",
) -> None:
    """Escalate for this job and record it on the halt's ``last`` record."""
    escalate(
        ctx.store,
        halt,
        note=note,
        diagnosis=verdict.cause if verdict else "",
        tried=tried,
        human_action=verdict.human_action if verdict else "",
        since=since,
        job_ref_id=ctx.ref_id,
    )
    save_last(
        ctx.store,
        halt.key,
        job=ctx.ref_id,
        outcome=outcome,
        at=datetime.now(UTC).isoformat(),
        cause=verdict.cause if verdict else "",
        human_action=verdict.human_action if verdict else "",
        reason="" if verdict else clip(tried),
    )


def _fail_and_escalate(
    ctx: Any, halt: HaltSpec, *, note: str, since: str, reason: str
) -> None:
    """The session produced no usable verdict: escalate with the reason and
    end the job failed. Escalating is attempted first and its own failure
    never hides the job failure."""
    escalated = True
    try:
        _escalate_job(
            ctx,
            halt,
            note=note,
            since=since,
            tried=f"a supervisor session (jo{ctx.ref_id}) gave no usable answer: {reason}",
            outcome="failed",
        )
    except Exception:
        escalated = False
        log.warning("halt_heal: escalation for %s failed", halt.key, exc_info=True)
    ctx.record_failure(
        f"halt_heal: {clip(reason)} — "
        + ("escalated" if escalated else "ESCALATION ALSO FAILED")
    )


def _supervise(ctx: Any, halt: HaltSpec, params: dict[str, Any], armed: str) -> None:
    # Resolved at call time so tests can monkeypatch ``router.route``.
    from precis import agentlog
    from precis.utils.llm.router import LlmRequest, route

    store = ctx.store
    since = str(params.get("halted_at") or "") or since_of(armed)
    note = armed or str(params.get("note") or "")

    base_prompt = _load_prompt()
    if base_prompt is None:
        raise RuntimeError("packaged halt-heal prompt unreadable")
    mcp_config = os.environ.get("PRECIS_MCP_CONFIG", "")
    if not mcp_config:
        # No tools, no evidence, no CLEAR: a session that cannot read the
        # fleet cannot show the cause gone.
        raise RuntimeError("PRECIS_MCP_CONFIG unset: the session cannot read the fleet")
    prompt = _compose_prompt(base_prompt, halt, note=note, halted_at=since)

    started = time.monotonic()
    log_id = agentlog.open_run(
        store,
        source=_SOURCE,
        title=f"halt_heal {halt.key}",
        prompt=prompt,
        job_ref_id=ctx.ref_id,
    )
    res = route(
        LlmRequest(
            tier=Tier.FRONTIER,
            source=_SOURCE,
            env_overlay=agentlog.run_env(log_id) or None,
            prompt=prompt,
            tools_needed=True,
            mcp_config=os.path.abspath(mcp_config),
            max_turns=_MAX_TURNS,
            timeout_s=_TIMEOUT_S,
            disallowed_tools=_DISALLOWED_TOOLS,
            output_format="stream-json",
            extra_args=("--verbose",),
            ref_id=ctx.ref_id,
        )
    )
    agentlog.close_run(
        store,
        log_id,
        status=(getattr(res, "terminal_reason", None) or "error")
        if res.error
        else "ok",
        res=res,
    )
    wall = res.duration_s if res.duration_s is not None else time.monotonic() - started
    _persist(ctx, res, wall_s=wall)

    if res.error:
        # A window-scoped pause parks THIS job until the window reopens; past
        # the park cap (or on a timeout) it falls through to the escalation.
        if defer_on_pause(ctx, res, label="halt_heal"):
            return
        _fail_and_escalate(
            ctx, halt, note=note, since=since, reason=f"session error: {res.error}"
        )
        return
    verdict = parse_verdict(res.text or "")
    if verdict is None:
        _fail_and_escalate(
            ctx,
            halt,
            note=note,
            since=since,
            reason="the reply did not end in the heal: verdict block",
        )
        return

    mode = heal_mode(store)
    if verdict.verdict == "CLEAR" and mode == "heal":
        if is_alerted(store, halt.key):
            # A human was already paged for this episode: the supervisor no
            # longer clears (this also covers a job queued before a newer
            # halt). The diagnosis rides the open alert.
            _escalate_job(
                ctx,
                halt,
                note=store.get_setting(halt.key) or note,
                since=since,
                tried=(
                    f"the session answered CLEAR (jo{ctx.ref_id}) after a human "
                    "was already alerted; nothing was cleared"
                ),
                verdict=verdict,
            )
            ctx.append_chunk(
                "job_event",
                "halt_heal: CLEAR after a human was already alerted; nothing cleared",
            )
            ctx.set_status("succeeded")
            return
        now_armed = store.get_setting(halt.key) or ""
        if now_armed and now_armed != armed:
            # Re-armed while the session ran: the cadence failed again, so
            # the cause the session judged gone is not.
            _escalate_job(
                ctx,
                halt,
                note=now_armed,
                since=since,
                tried=(
                    f"the session answered CLEAR (jo{ctx.ref_id}) but the "
                    "halt was re-armed while it ran"
                ),
                verdict=verdict,
            )
            ctx.append_chunk(
                "job_event", "halt_heal: CLEAR, but the halt re-armed; escalated"
            )
            ctx.set_status("succeeded")
            return
        if not now_armed:
            # A probe or an operator cleared it while the session ran: the
            # supervisor cleared nothing and must not claim it did.
            save_last(
                store,
                halt.key,
                job=ctx.ref_id,
                outcome="already-clear",
                cause=verdict.cause,
            )
            ctx.append_chunk(
                "job_event",
                "halt_heal: CLEAR, but the halt was already empty (a probe or an "
                "operator cleared it first); nothing to clear",
            )
            ctx.set_status("succeeded")
            return
        store.set_setting(halt.key, "")
        save_last(
            store,
            halt.key,
            job=ctx.ref_id,
            outcome="cleared",
            cleared_at=datetime.now(UTC).isoformat(),
            cause=verdict.cause,
        )
        ctx.append_chunk(
            "job_event",
            f"halt_heal: cleared {halt.key} (model diagnosis: {verdict.cause}; "
            f"evidence: {' | '.join(verdict.evidence)})",
        )
        ctx.set_status("succeeded")
        return

    if verdict.verdict == "CLEAR":
        tried = (
            f"the session answered CLEAR (jo{ctx.ref_id}) but the mode is "
            f"'{mode}', so nothing was cleared"
        )
    else:
        tried = (
            f"a supervisor session (jo{ctx.ref_id}) investigated and could not clear it"
        )
    _escalate_job(ctx, halt, note=note, since=since, tried=tried, verdict=verdict)
    ctx.append_chunk(
        "job_event",
        f"halt_heal: escalated ({verdict.verdict}, mode {mode}; model diagnosis: "
        f"{verdict.cause}; evidence: {' | '.join(verdict.evidence)})",
    )
    ctx.set_status("succeeded")


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher: one session, then clear or escalate.

    Terminal outcomes: unknown halt key → failed (nothing registered to
    escalate for); the halt already empty when the job starts (a probe or an
    operator beat it) → succeeded without a session; mode ``off`` → escalated
    without a session, succeeded; a verdict → succeeded
    (cleared, or escalated); no verdict / error / exception → escalated and
    failed; a quota pause → parked, same job.
    """
    params = (ctx.meta or {}).get("params") or {}
    key = str(params.get("key") or "")
    halt = HALTS.get(key)
    if halt is None:
        ctx.record_failure(f"halt_heal: no registered halt for key {key!r}")
        return
    armed = ""
    try:
        armed = ctx.store.get_setting(key) or ""
        if not armed:
            ctx.append_chunk(
                "job_event",
                "halt_heal: the halt was already cleared before the session "
                "started; nothing to do",
            )
            ctx.set_status("succeeded")
            return
        if heal_mode(ctx.store) == "off":
            # Switched off after this job was queued: no session, a human.
            _escalate_job(
                ctx,
                halt,
                note=armed,
                since=str(params.get("halted_at") or ""),
                tried="nothing: the supervisor is switched off",
            )
            ctx.append_chunk(
                "job_event",
                "halt_heal: the supervisor is switched off; escalated without a session",
            )
            ctx.set_status("succeeded")
            return
        _supervise(ctx, halt, params, armed)
    except Exception as exc:
        log.warning("halt_heal: %s session raised", key, exc_info=True)
        _fail_and_escalate(
            ctx,
            halt,
            note=armed or str(params.get("note") or ""),
            since=str(params.get("halted_at") or ""),
            reason=mask_secrets(f"{type(exc).__name__}: {exc}"),
        )


SPEC = JobTypeSpec(
    name="halt_heal",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "Verdict",
    "load",
    "parse_verdict",
]
