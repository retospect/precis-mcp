"""The runner seam: fake (replay + record) and live (``claude -p`` dry run).

Both runners receive the same prompt — :func:`build_prompt` renders the
injected skill(s), the task and the JSON reply contract — so a test that
asserts on the fake's recorded prompt is asserting on what the live model
would have read.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from precis.skill_eval.cases import VERBS, EvalCase, Transcript, VerbCall
from precis.utils._claude_subprocess import ClaudeProcessError, resolve_binary
from precis.utils.claude_oauth import LOGGED_OUT_MARKERS

log = logging.getLogger(__name__)

#: Default caps for the live runner. Env knobs (Tier 2, read once here):
#: ``PRECIS_SKILL_EVAL_CASE_USD``, ``PRECIS_SKILL_EVAL_RUN_USD``,
#: ``PRECIS_SKILL_EVAL_TIMEOUT_S``, ``PRECIS_SKILL_EVAL_MODEL``.
DEFAULT_CASE_USD = 0.25
DEFAULT_RUN_USD = 2.00
DEFAULT_TIMEOUT_S = 180.0

_AUTH_MARKERS: tuple[str, ...] = (
    *LOGGED_OUT_MARKERS,
    "invalid authentication credentials",
    "failed to authenticate",
    "authentication_error",
)


class LiveUnavailable(RuntimeError):
    """``claude`` is missing or unauthenticated: the run is skipped, never
    scored — an unauthenticated container ``claude`` exits 0 with a banner
    and would otherwise read as a failed (or, worse, passed) case."""


class RunBudgetExhausted(RuntimeError):
    """The per-run USD cap would be exceeded by the next case."""


@dataclass(frozen=True, slots=True)
class RecordedRun:
    case_id: str
    prompt: str


class Runner(Protocol):
    name: str

    def run(self, case: EvalCase, prompt: str) -> Transcript: ...


def build_prompt(case: EvalCase, skill_texts: Mapping[str, str]) -> str:
    """The dry-run prompt: skills in full, the task, one-JSON-object reply."""
    skills = "\n\n".join(
        f"<skill id={slug!r}>\n{skill_texts[slug]}\n</skill>" for slug in case.skills
    )
    verbs = ", ".join(sorted(VERBS))
    return (
        "You are an agent working through the precis MCP server (verbs: "
        f"{verbs}). This is a dry run: you cannot execute calls. Read the "
        "skill(s) below, then plan exactly how you would complete the task.\n\n"
        f"{skills}\n\n"
        f"Task: {case.prompt}\n\n"
        "Reply with exactly one JSON object and nothing else:\n"
        '{"calls": [{"verb": "get", "kind": "paper", "args": {"id": "pa40"}}], '
        '"record": null, "answer": "..."}\n'
        "- calls: the precis verb calls you would make, in order, with the exact "
        "arguments (kind in 'kind', every other parameter in 'args').\n"
        "- record: if the task asks you to create something, an object with "
        "the 'kind' and the fields of the one record you would put; else null.\n"
        "- answer: the one-paragraph reply you would give the user."
    )


class FakeRunner:
    """Replays each case's scripted ``fake:`` transcript (or an override from
    ``scripts``) and records every prompt it was handed."""

    name = "fake"

    def __init__(self, scripts: Mapping[str, Transcript] | None = None) -> None:
        self.scripts: dict[str, Transcript] = dict(scripts or {})
        self.recorded: list[RecordedRun] = []

    def run(self, case: EvalCase, prompt: str) -> Transcript:
        self.recorded.append(RecordedRun(case.case_id, prompt))
        return self.scripts.get(case.case_id, case.fake)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def parse_transcript(data: Mapping[str, Any], *, cost_usd: float | None) -> Transcript:
    """The model's JSON reply → :class:`Transcript`. Lenient on shape: a
    malformed call is dropped with a log, never a crash, so a sloppy reply
    scores as the calls it did declare."""
    calls: list[VerbCall] = []
    for raw in data.get("calls") or []:
        if not isinstance(raw, dict) or raw.get("verb") not in VERBS:
            log.warning("skill eval: dropping malformed call %r", raw)
            continue
        args = raw.get("args")
        kind = raw.get("kind")
        calls.append(
            VerbCall(
                verb=str(raw["verb"]),
                kind=str(kind) if kind is not None else None,
                args=dict(args) if isinstance(args, dict) else {},
            )
        )
    record = data.get("record")
    return Transcript(
        calls=tuple(calls),
        record=dict(record) if isinstance(record, dict) else None,
        answer=str(data.get("answer") or ""),
        cost_usd=cost_usd,
    )


class LiveClaudeRunner:
    """Planned-call dry run through tool-free ``claude -p`` (host only).

    ``call`` defaults to :func:`precis.utils.claude_p.call_claude_p`; tests
    inject a stub. Cost accounting is conservative: a reply without a
    ``total_cost_usd`` is booked at the per-case cap.
    """

    name = "live"

    def __init__(
        self,
        *,
        model: str | None = None,
        case_usd: float | None = None,
        run_usd: float | None = None,
        timeout_s: float | None = None,
        call: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model or os.environ.get("PRECIS_SKILL_EVAL_MODEL") or None
        self.case_usd = (
            case_usd
            if case_usd is not None
            else _env_float("PRECIS_SKILL_EVAL_CASE_USD", DEFAULT_CASE_USD)
        )
        self.run_usd = (
            run_usd
            if run_usd is not None
            else _env_float("PRECIS_SKILL_EVAL_RUN_USD", DEFAULT_RUN_USD)
        )
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else _env_float("PRECIS_SKILL_EVAL_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        )
        if call is None:
            from precis.utils.claude_p import call_claude_p

            call = call_claude_p
        self._call = call
        self.spent_usd = 0.0

    def unavailable_reason(self) -> str | None:
        """Pre-flight: a reason string when no run should even start."""
        binary = resolve_binary()
        if shutil.which(binary) is None and not os.path.isfile(binary):
            return f"claude binary not found ({binary!r})"
        return None

    def run(self, case: EvalCase, prompt: str) -> Transcript:
        if self.spent_usd + self.case_usd > self.run_usd + 1e-9:
            raise RunBudgetExhausted(
                f"run budget ${self.run_usd:.2f} would be exceeded "
                f"(spent ${self.spent_usd:.2f}, next case cap ${self.case_usd:.2f})"
            )
        try:
            res = self._call(
                prompt,
                model=self.model,
                max_usd=self.case_usd,
                timeout_s=self.timeout_s,
            )
        except ClaudeProcessError as exc:
            if exc.binary_missing:
                raise LiveUnavailable(str(exc)) from exc
            blob = " ".join(
                t for t in (str(exc), exc.stdout or "", exc.stderr or "") if t
            ).lower()
            if any(m in blob for m in _AUTH_MARKERS):
                raise LiveUnavailable(f"claude is not authenticated: {exc}") from exc
            raise
        cost = res.cost_usd
        self.spent_usd += cost if cost is not None else self.case_usd
        data = res.data if isinstance(res.data, dict) else {}
        if not data:
            try:
                data = json.loads(res.text or "")
            except (TypeError, ValueError):
                data = {}
        return parse_transcript(data, cost_usd=cost)


__all__ = [
    "DEFAULT_CASE_USD",
    "DEFAULT_RUN_USD",
    "DEFAULT_TIMEOUT_S",
    "FakeRunner",
    "LiveClaudeRunner",
    "LiveUnavailable",
    "RecordedRun",
    "RunBudgetExhausted",
    "Runner",
    "build_prompt",
    "parse_transcript",
]
