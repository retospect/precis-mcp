"""Drive cases through a runner; build the JSON report and the short table."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.skill_eval.cases import EvalCase, Transcript
from precis.skill_eval.criteria import check
from precis.skill_eval.runners import (
    LiveUnavailable,
    RunBudgetExhausted,
    Runner,
    build_prompt,
)

log = logging.getLogger(__name__)

DEFAULT_REPORT_PATH = ".skill-eval-report.json"


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    skill: str
    #: ``pass`` | ``fail`` | ``error`` | ``skip``
    status: str
    reasons: tuple[str, ...] = ()
    cost_usd: float | None = None
    calls: tuple[str, ...] = ()


@dataclass(slots=True)
class Report:
    runner: str
    #: ``ok`` (cases ran) | ``skipped`` (no case ran: runner unavailable, no cases)
    status: str = "ok"
    reason: str | None = None
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    cases: list[CaseResult] = field(default_factory=list)
    spent_usd: float = 0.0

    def count(self, status: str) -> int:
        return sum(1 for c in self.cases if c.status == status)

    @property
    def red(self) -> bool:
        return any(c.status in ("fail", "error") for c in self.cases)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["summary"] = {s: self.count(s) for s in ("pass", "fail", "error", "skip")}
        return d

    def write_json(self, path: str | Path = DEFAULT_REPORT_PATH) -> Path:
        p = Path(path)
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return p

    def render_table(self) -> str:
        if self.status != "ok":
            return f"skill-eval [{self.runner}]: skipped — {self.reason}"
        width = max((len(c.case_id) for c in self.cases), default=8)
        lines = [f"skill-eval [{self.runner}] {self.started_at}"]
        for c in self.cases:
            cost = f"${c.cost_usd:.3f}" if c.cost_usd is not None else "-"
            note = "; ".join(c.reasons)
            lines.append(
                f"{c.case_id:<{width}}  {c.status.upper():<5}  {cost:>7}  {note}"
            )
        s = self.to_dict()["summary"]
        lines.append(
            f"{s['pass']} pass, {s['fail']} fail, {s['error']} error, "
            f"{s['skip']} skip; spent ${self.spent_usd:.2f}"
        )
        return "\n".join(lines)


def shipped_skill_text(slug: str) -> str:
    """Skill body with include directives expanded (what injection serves)."""
    from precis.handlers.skill import _load_skill

    text = _load_skill(slug)
    if text is None:
        raise KeyError(slug)
    return text


def run_cases(
    cases: Iterable[EvalCase],
    runner: Runner,
    *,
    skill_text: Callable[[str], str] = shipped_skill_text,
) -> Report:
    """Run every case through ``runner`` and judge each transcript.

    A :class:`LiveUnavailable` from the runner marks the whole run
    ``skipped`` (nothing scored); :class:`RunBudgetExhausted` skips the
    remaining cases; any other exception is that case's ``error``.
    """
    report = Report(runner=runner.name)
    pending = list(cases)
    if not pending:
        report.status, report.reason = "skipped", "no cases selected"
        return report
    unavailable = getattr(runner, "unavailable_reason", lambda: None)()
    if unavailable:
        report.status, report.reason = "skipped", unavailable
        return report
    budget_out: str | None = None
    for case in pending:
        if budget_out is not None:
            report.cases.append(
                CaseResult(case.case_id, case.skill, "skip", (budget_out,))
            )
            continue
        try:
            prompt = build_prompt(case, {s: skill_text(s) for s in case.skills})
            transcript: Transcript = runner.run(case, prompt)
        except LiveUnavailable as exc:
            report.status, report.reason, report.cases = "skipped", str(exc), []
            return report
        except RunBudgetExhausted as exc:
            budget_out = str(exc)
            report.cases.append(
                CaseResult(case.case_id, case.skill, "skip", (budget_out,))
            )
            continue
        except Exception as exc:  # one case's crash must not void the run
            log.warning("skill eval: case %s raised: %s", case.case_id, exc)
            report.cases.append(
                CaseResult(case.case_id, case.skill, "error", (str(exc),))
            )
            continue
        verdict = check(case.criterion, transcript)
        if transcript.cost_usd is not None:
            report.spent_usd += transcript.cost_usd
        report.cases.append(
            CaseResult(
                case.case_id,
                case.skill,
                "pass" if verdict.passed else "fail",
                verdict.reasons,
                transcript.cost_usd,
                tuple(c.render() for c in transcript.calls),
            )
        )
    return report


__all__ = [
    "DEFAULT_REPORT_PATH",
    "CaseResult",
    "Report",
    "run_cases",
    "shipped_skill_text",
]
