"""Drive cases through a runner; build the JSON report and the short table."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.skill_eval.cases import EvalCase, Transcript, discovery_metrics
from precis.skill_eval.criteria import check
from precis.skill_eval.runners import (
    LiveUnavailable,
    RunBudgetExhausted,
    Runner,
    build_prompt,
)

log = logging.getLogger(__name__)

DEFAULT_REPORT_PATH = ".skill-eval-report.json"

#: The paired-run failure classes (gr634773). ``retrieval`` = the skill
#: works when handed over but the agent failed to find it; ``guidance`` =
#: the run fails even with the skill supplied, with clean tool calls;
#: ``tool`` = precis calls errored or the runner crashed; ``unattributed``
#: = contradictory or partial evidence, never a root cause on its own.
FAILURE_CLASSES = ("retrieval", "guidance", "tool", "unattributed")


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    skill: str
    #: ``pass`` | ``fail`` | ``error`` | ``skip``
    status: str
    reasons: tuple[str, ...] = ()
    cost_usd: float | None = None
    calls: tuple[str, ...] = ()
    #: Declared task depth (the case's ``subtasks:``, default 1) — the
    #: accuracy-by-depth bucket.
    subtasks: int = 1
    #: The model that ran the case (the runner's ``.model``); ``None`` on
    #: the fake runner or the runner's default model.
    model: str | None = None
    #: Which condition produced the row: ``supplied`` (skill text injected)
    #: or ``discovery`` (no skill text, no slug) — gr634773.
    condition: str = "supplied"
    #: Discovery exploration before the first correct call — the number of
    #: calls and the result bytes they read (``None`` on supplied rows and
    #: runners that can't measure it).
    discovery_calls: int | None = None
    discovery_bytes: int | None = None
    #: Paired-run failure class (both rows of a failed pair carry it);
    #: ``None`` when the pair passed or no pairing ran.
    failure: str | None = None
    #: The runner's final answer — the retained transcript's reply half.
    answer: str = ""


def _errored(row: CaseResult) -> bool:
    """Tool-surface failure evidence: a crashed run or a call whose tool
    result came back an error."""
    return row.status == "error" or any(
        r.startswith("call errored:") for r in row.reasons
    )


def classify_failure(supplied: CaseResult, discovery: CaseResult) -> str:
    """Classify one failed supplied/discovery pair (gr634773).

    Neither condition failing alone proves a root cause, so the classes
    are conservative: the skill passing *when supplied* pins a discovery
    failure on retrieval; a clean double failure pins guidance; errored
    calls or a crashed runner pin the tool surface; everything else —
    skips, errors, contradictions — stays ``unattributed``.
    """
    if discovery.status == "pass":
        # Includes "no failure at all" (both pass) and the contradictory
        # supplied-fail/discovery-pass — neither attributes a cause.
        return "unattributed"
    if supplied.status == "pass":
        return "retrieval"
    if "skip" in (supplied.status, discovery.status):
        return "unattributed"
    if _errored(supplied) or _errored(discovery):
        return "tool"
    return "guidance"


def classify_pairs(cases: list[CaseResult]) -> dict[tuple[str | None, str], str]:
    """Pair supplied/discovery rows by ``(model, case_id)``, stamp
    ``failure`` on both rows of each failed pair, and return the
    classifications. Rows whose counterpart is missing (a skipped
    condition) are left unstamped."""
    supplied = {
        (c.model, c.case_id): i
        for i, c in enumerate(cases)
        if c.condition == "supplied"
    }
    out: dict[tuple[str | None, str], str] = {}
    for i, c in enumerate(cases):
        if c.condition != "discovery":
            continue
        j = supplied.get((c.model, c.case_id))
        if j is None:
            continue
        s = cases[j]
        if s.status == "pass" and c.status == "pass":
            continue
        cls = classify_failure(s, c)
        out[(c.model, c.case_id)] = cls
        cases[i] = replace(c, failure=cls)
        cases[j] = replace(s, failure=cls)
    return out


@dataclass(slots=True)
class Report:
    runner: str
    #: ``ok`` (cases ran) | ``skipped`` (no case ran: runner unavailable, no cases)
    status: str = "ok"
    reason: str | None = None
    #: ``supplied`` | ``discovery`` | ``paired`` — how prompts were built
    #: (gr634773); ``paired`` means each case ran under both conditions.
    condition: str = "supplied"
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

    def accuracy_by_depth(self) -> dict[str, dict[int, dict[str, int]]]:
        """``{model: {subtasks: {"pass": n, "total": m}}}`` over scored
        cases (``pass``/``fail`` only — skips and errors are not accuracy
        evidence). The llm scorecard shape for "how often does an agent
        finish an N-call job" (gr608716)."""
        out: dict[str, dict[int, dict[str, int]]] = {}
        for c in self.cases:
            if c.status not in ("pass", "fail"):
                continue
            bucket = out.setdefault(c.model or "default", {}).setdefault(
                c.subtasks, {"pass": 0, "total": 0}
            )
            bucket["total"] += 1
            bucket["pass"] += c.status == "pass"
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["summary"] = {s: self.count(s) for s in ("pass", "fail", "error", "skip")}
        d["accuracy_by_depth"] = self.accuracy_by_depth()
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
            notes = list(c.reasons)
            if c.discovery_calls is not None:
                notes.append(
                    f"discovery {c.discovery_calls} calls/"
                    f"{c.discovery_bytes}B to first correct call"
                )
            note = "; ".join(notes)
            lines.append(
                f"{c.case_id:<{width}}  {c.status.upper():<5}  {cost:>7}  {note}"
            )
        s = self.to_dict()["summary"]
        depth = self.accuracy_by_depth()
        if depth and (
            self.runner == "executed"
            or len(depth) > 1
            or any(c.subtasks > 1 for c in self.cases)
        ):
            lines.append("accuracy by subtask count:")
            width = max(len(m) for m in depth)
            for model in sorted(depth):
                for st in sorted(depth[model]):
                    b = depth[model][st]
                    lines.append(
                        f"  {model:<{width}}  {st} subtasks  "
                        f"{b['pass']}/{b['total']}  ({b['pass'] / b['total']:.0%})"
                    )
        if self.condition == "paired":
            pairs: dict[tuple[str | None, str], dict[str, CaseResult]] = {}
            for c in self.cases:
                pairs.setdefault((c.model, c.case_id), {})[c.condition] = c
            failed = {
                key: rows
                for key, rows in pairs.items()
                if any(r.failure is not None for r in rows.values())
            }
            if failed:
                lines.append("paired failures:")
                for pmodel, cid in sorted(failed, key=lambda k: (k[1], k[0] or "")):
                    rows = failed[(pmodel, cid)]
                    s_row = rows.get("supplied")
                    d_row = rows.get("discovery")
                    fail_cls = next(
                        (r.failure for r in rows.values() if r.failure is not None),
                        None,
                    )
                    lines.append(
                        f"  {cid}  supplied={s_row.status.upper() if s_row else '-'}"
                        f" discovery={d_row.status.upper() if d_row else '-'}"
                        f"  {fail_cls}"
                    )
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
    remaining cases; any other exception is that case's ``error``. The
    report's ``condition`` comes from the runner (``supplied`` unless it
    declares one); discovery-condition rows carry the exploration metrics
    — calls and result bytes before the first correct call.
    """
    report = Report(
        runner=runner.name, condition=getattr(runner, "condition", "supplied")
    )
    pending = list(cases)
    if not pending:
        report.status, report.reason = "skipped", "no cases selected"
        return report
    unavailable = getattr(runner, "unavailable_reason", lambda: None)()
    if unavailable:
        report.status, report.reason = "skipped", unavailable
        return report
    model = getattr(runner, "model", None)
    # A runner may own its prompt contract (the executed runner's *call
    # the tools* brief vs. the dry-run's declared-JSON one); a runner
    # without one still gets the dry-run builder, honouring its condition.
    prompt_fn = getattr(runner, "build_prompt", None)
    if prompt_fn is None:
        condition = getattr(runner, "condition", "supplied")

        def prompt_fn(case: EvalCase, texts: dict[str, str]) -> str:
            return build_prompt(case, texts, condition=condition)

    budget_out: str | None = None
    for case in pending:
        if budget_out is not None:
            report.cases.append(
                CaseResult(
                    case.case_id,
                    case.skill,
                    "skip",
                    (budget_out,),
                    subtasks=case.subtasks,
                    model=model,
                    condition=report.condition,
                )
            )
            continue
        try:
            prompt = prompt_fn(case, {s: skill_text(s) for s in case.skills})
            transcript: Transcript = runner.run(case, prompt)
        except LiveUnavailable as exc:
            report.status, report.reason, report.cases = "skipped", str(exc), []
            return report
        except RunBudgetExhausted as exc:
            budget_out = str(exc)
            report.cases.append(
                CaseResult(
                    case.case_id,
                    case.skill,
                    "skip",
                    (budget_out,),
                    subtasks=case.subtasks,
                    model=model,
                    condition=report.condition,
                )
            )
            continue
        except Exception as exc:  # one case's crash must not void the run
            log.warning("skill eval: case %s raised: %s", case.case_id, exc)
            report.cases.append(
                CaseResult(
                    case.case_id,
                    case.skill,
                    "error",
                    (str(exc),),
                    subtasks=case.subtasks,
                    model=model,
                    condition=report.condition,
                )
            )
            continue
        verdict = check(case.criterion, transcript)
        if transcript.cost_usd is not None:
            report.spent_usd += transcript.cost_usd
        dc = db = None
        if report.condition == "discovery":
            dc, db = discovery_metrics(case.criterion, transcript)
        report.cases.append(
            CaseResult(
                case.case_id,
                case.skill,
                "pass" if verdict.passed else "fail",
                verdict.reasons,
                transcript.cost_usd,
                tuple(c.render() for c in transcript.calls),
                subtasks=case.subtasks,
                model=model,
                condition=report.condition,
                discovery_calls=dc,
                discovery_bytes=db,
                answer=transcript.answer,
            )
        )
    return report


__all__ = [
    "DEFAULT_REPORT_PATH",
    "FAILURE_CLASSES",
    "CaseResult",
    "Report",
    "classify_failure",
    "classify_pairs",
    "run_cases",
    "shipped_skill_text",
]
