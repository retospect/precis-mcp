"""Competency questions: load from a quest, search, score, alert.

A quest's ``meta.competency_questions`` is a list of ``{question, expected,
forbidden}`` rows (record handles of kind finding / measure / material /
paper). :func:`load_questions` validates one row at a time (a bad row is a
problem string, never fatal; a handle must be well-formed and name an
allowed kind, and may point at a since-retired record, so a dropped
expected record shows as a regression instead of an invalid row).
:func:`answer_handles` runs the
retrieval-eval chunk search and maps hits to ref handles; :func:`score`
grades one answer; :func:`run_competency` writes ``meta.competency_last``
and raises ``competency`` alerts (forbidden, regression, missing). Prod
handles live on the quest in prod, never in the repo. A ``measure`` row is
not a ref (no chunks), so search can never return one: an expected measure
reads ``warn`` until a measure-aware answer path exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from precis.utils import handle_registry
from precis.utils.handle_registry import format_handle

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

    from precis.store.store import Store
    from precis.store.types import Ref

_ANSWER_KINDS = frozenset({"finding", "measure", "material", "paper"})


def _measure_handle(token: str) -> str | None:
    """Canonical ``mx<N>`` handle, or ``None``.

    A measure lives in its own table (``measures.id``), so
    ``handle_registry.parse`` cannot decode one — the shape check mirrors
    ``handlers._measure_write.coerce_measure_id``.
    """
    text = token.strip()
    code = handle_registry.code_for_kind("measure")
    if text[: len(code)].lower() == code and text[len(code) :].isdecimal():
        number = int(text[len(code) :])
        if number > 0:
            return f"{code}{number}"
    return None


@dataclass(frozen=True, slots=True)
class CompetencyQuestion:
    """Question text and canonical record handles for scoring."""

    question: str
    expected: tuple[str, ...]
    forbidden: tuple[str, ...] = ()


def _load_handles(field: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of record handles")
    handles = []
    for token in value:
        if not isinstance(token, str):
            raise ValueError(f"{field} must contain strings")
        parsed = handle_registry.parse(token)
        if parsed is not None and not parsed[1] and parsed[0] in _ANSWER_KINDS:
            handles.append(format_handle(parsed[0], parsed[2]))
            continue
        measure = _measure_handle(token) if parsed is None else None
        if measure is not None:
            handles.append(measure)
            continue
        raise ValueError(
            f"{field} handle {token!r} must be a finding, measure, "
            "material or paper record handle"
        )
    return tuple(handles)


def load_questions(
    store: Store, quest_ref: Ref
) -> tuple[list[CompetencyQuestion], list[str]]:
    """Return valid questions and problems indexed by one-based metadata row."""
    rows = quest_ref.meta.get("competency_questions", [])
    if not isinstance(rows, list):
        return [], ["competency_questions must be a list"]

    questions: list[CompetencyQuestion] = []
    problems: list[str] = []
    for index, row in enumerate(rows, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("question row must be an object")
            question = row.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("question must be a non-empty string")
            expected = _load_handles("expected", row.get("expected"))
            forbidden = _load_handles("forbidden", row.get("forbidden", []))
        except ValueError as exc:
            problems.append(f"competency_questions row {index}: {exc}")
        else:
            questions.append(CompetencyQuestion(question, expected, forbidden))
    return questions, problems


def score(
    question: CompetencyQuestion,
    answer_handles: Iterable[str],
    last_found: Iterable[str],
) -> Literal["pass", "warn", "fail", "regressed"]:
    """Compare an answer with this question's expectations and prior hits."""
    answer = set(answer_handles)
    if answer.intersection(question.forbidden):
        return "fail"
    missing = set(question.expected) - answer
    if missing.intersection(last_found):
        return "regressed"
    return "warn" if missing else "pass"


def answer_handles(
    store: Store, question: CompetencyQuestion, k: int, embedder: Any | None
) -> list[str]:
    """Ref handles the ``search`` chunk retrieval returns, rank order, deduped."""
    from precis.mesh_eval.retrieval import search_chunk_triples

    out: list[str] = []
    for _block, ref, _score in search_chunk_triples(
        store, question.question, k, embedder
    ):
        handle = handle_registry.try_format(ref.kind, int(ref.id))
        if handle is not None and handle not in out:
            out.append(handle)
    return out


@dataclass(frozen=True, slots=True)
class QuestionResult:
    """One question's status plus what drove it."""

    question: str
    status: Literal["pass", "warn", "fail", "regressed"]
    forbidden_hits: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()


def _quest_label(quest_ref: Ref) -> str:
    return format_handle("quest", int(quest_ref.id))


def evaluate(
    store: Store,
    quest_ref: Ref,
    *,
    k: int = 30,
    embedder: Any | None = None,
) -> tuple[list[QuestionResult], dict[str, list[str]], list[str]]:
    """Score every valid question; return ``(results, found_now, problems)``.

    ``found_now`` maps question text to the expected handles that count as
    found for the next run's regression check: yesterday's finds that are
    still gone stay in it, so a regression keeps reading ``regressed``
    until the handle is back or the question is edited.
    """
    questions, problems = load_questions(store, quest_ref)
    last = quest_ref.meta.get("competency_last")
    prior = last.get("found", {}) if isinstance(last, dict) else {}
    results: list[QuestionResult] = []
    found_now: dict[str, list[str]] = {}
    for q in questions:
        answer = answer_handles(store, q, k, embedder)
        last_found = [h for h in prior.get(q.question, []) if h in q.expected]
        status = score(q, answer, last_found)
        present = set(answer)
        results.append(
            QuestionResult(
                q.question,
                status,
                tuple(h for h in q.forbidden if h in present),
                tuple(h for h in q.expected if h not in present),
                tuple(h for h in last_found if h not in present),
            )
        )
        found_now[q.question] = [
            h for h in q.expected if h in present or h in last_found
        ]
    return results, found_now, problems


_ALERT_TITLES = {
    "fail": ("forbidden", "forbidden handle in answer", "critical"),
    "regressed": ("regression", "expected handle dropped out", "critical"),
    "warn": ("missing", "expected handle missing", "warn"),
}


def run_competency(
    store: Store,
    quest_ref: Ref,
    *,
    k: int = 30,
    embedder: Any | None = None,
) -> list[QuestionResult]:
    """Run one quest's questions, write ``meta.competency_last``, alert.

    One alert per (kind, quest), listing every affected question.
    """
    from datetime import UTC, datetime

    from precis.alerts import raise_alert

    results, found_now, problems = evaluate(store, quest_ref, k=k, embedder=embedder)
    quest = _quest_label(quest_ref)
    for status, (rule, title, severity) in _ALERT_TITLES.items():
        hit = [r for r in results if r.status == status]
        if not hit:
            continue
        lines = []
        for r in hit:
            handles = r.forbidden_hits or r.lost or r.missing
            lines.append(f"- {r.question} [{' '.join(handles)}]")
        raise_alert(
            store,
            source="competency",
            fingerprint=f"{rule}:{quest}",
            title=f"{quest}: {title} ({len(hit)} question(s))",
            detail="\n".join(lines),
            severity=severity,
            subject_ref_id=int(quest_ref.id),
        )
    store.update_ref(
        int(quest_ref.id),
        meta_patch={
            "competency_last": {
                "at": datetime.now(UTC).isoformat(),
                "found": found_now,
                "status": {r.question: r.status for r in results},
                "problems": problems,
            }
        },
    )
    return results


def competency_quests(store: Store) -> list[Ref]:
    """Every live quest that carries ``meta.competency_questions``."""
    return store.list_refs(kind="quest", meta_has="competency_questions", limit=None)


def run_all(store: Store, *, embedder: Any | None = None) -> int:
    """Run every competency set; return the number of quests run."""
    quests = competency_quests(store)
    for quest in quests:
        run_competency(store, quest, embedder=embedder)
    return len(quests)


def format_results(results: list[QuestionResult], problems: list[str]) -> str:
    """Plain-text per-question table."""
    lines = [f"{r.status:<9} {r.question}" for r in results]
    lines += [f"invalid   {p}" for p in problems]
    return "\n".join(lines)


def resolve_quests(store: Store, quest: str | None) -> list[Ref]:
    """The named quest handle's ref, or every competency set when ``None``."""
    if quest is None:
        return competency_quests(store)
    resolved = store.resolve_handle(quest)
    ref = (
        store.get_ref(kind="quest", id=resolved.ref_id)
        if resolved is not None and resolved.kind == "quest"
        else None
    )
    if ref is None:
        raise ValueError(f"{quest!r} is not a live quest handle")
    return [ref]


def report(
    store: Store,
    quest: str | None,
    *,
    as_json: bool = False,
    k: int = 30,
    embedder: Any | None = None,
) -> str:
    """Read-only per-question status for the CLI: no meta write, no alerts."""
    import json

    out: list[dict[str, Any]] = []
    text: list[str] = []
    for ref in resolve_quests(store, quest):
        results, _found, problems = evaluate(store, ref, k=k, embedder=embedder)
        label = _quest_label(ref)
        out.append(
            {
                "quest": label,
                "results": [
                    {
                        "question": r.question,
                        "status": r.status,
                        "forbidden_hits": list(r.forbidden_hits),
                        "missing": list(r.missing),
                        "lost": list(r.lost),
                    }
                    for r in results
                ],
                "problems": problems,
            }
        )
        text.append(label)
        text.append(format_results(results, problems))
    return json.dumps(out, indent=1) if as_json else "\n".join(text)


def draft(
    store: Store,
    quest: str,
    *,
    k: int = 10,
    embedder: Any | None = None,
) -> str:
    """Candidate expected handles per question, model-drafted, written nowhere."""
    lines = ["# model-drafted candidates: review before writing to prod"]
    for ref in resolve_quests(store, quest):
        rows = ref.meta.get("competency_questions", [])
        for row in rows if isinstance(rows, list) else []:
            text = row.get("question") if isinstance(row, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            cands = [
                h
                for h in answer_handles(
                    store, CompetencyQuestion(text, ()), k, embedder
                )
                if (handle_registry.parse(h) or ("",))[0] in _ANSWER_KINDS
            ]
            lines.append(f"{text}\n  candidates: {' '.join(cands) or '(none)'}")
    return "\n".join(lines)
