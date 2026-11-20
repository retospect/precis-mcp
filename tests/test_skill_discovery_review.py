"""The advisory discovery sampler (``precis.skill_eval.discovery_review``):
case selection priority, retained-report failure parsing and repair
briefs. Hermetic: no DB, no model, no git — the git/subprocess seams are
thin wrappers over ``select_review_cases``/``failed_case_ids_from_reports``,
which these tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.skill_eval import CallPattern, Criterion, EvalCase, Transcript, VerbCall
from precis.skill_eval.discovery_review import (
    failed_case_ids_from_reports,
    latest_stamp,
    repair_briefs,
    select_review_cases,
)
from precis.skill_eval.report import CaseResult


def _case(skill: str, case_id: str) -> EvalCase:
    return EvalCase(
        case_id,
        skill,
        (skill,),
        f"task {case_id}",
        Criterion(calls=(CallPattern("get", "paper"),)),
        Transcript(calls=(VerbCall("get", "paper"),)),
    )


# Three families: alpha (3 cases), beta (2), gamma (2). One alpha case
# was previously failed; the beta skill changed.
_ALPHA = [
    _case("precis-alpha-help", "alpha/one"),
    _case("precis-alpha-help", "alpha/two"),
    _case("precis-alpha-help", "alpha/three"),
]
_BETA = [_case("precis-beta-help", "beta/one"), _case("precis-beta-help", "beta/two")]
_GAMMA = [
    _case("precis-gamma-help", "gamma/one"),
    _case("precis-gamma-help", "gamma/two"),
]
_CORPUS = [*_ALPHA, *_BETA, *_GAMMA]


def test_selector_prioritises_changed_then_failed_then_undertested() -> None:
    changed = {"precis-beta-help"}
    failed = {"alpha/two"}
    # Tier 1 fills the quota: only changed-skill cases.
    picked = select_review_cases(
        _CORPUS, changed_skills=changed, failed_case_ids=failed, limit=2
    )
    assert [c.case_id for c in picked] == ["beta/one", "beta/two"]
    picked = select_review_cases(
        _CORPUS,
        changed_skills=changed,
        failed_case_ids=failed,
        limit=4,
        run_counts={"alpha/one": 3},
    )
    assert [c.case_id for c in picked] == [
        "beta/one",
        "beta/two",
        "alpha/two",
        "gamma/one",
    ]
    first = select_review_cases(_CORPUS, limit=3)
    second = select_review_cases(
        _CORPUS,
        limit=3,
        run_counts={c.case_id: 1 for c in first},
    )
    assert not {c.case_id for c in first} & {c.case_id for c in second}
    # A changed case that also failed is picked once, in tier 1 order.
    picked = select_review_cases(
        _CORPUS,
        changed_skills={"precis-alpha-help"},
        failed_case_ids={"alpha/two"},
        limit=7,
    )
    assert [c.case_id for c in picked] == [
        "alpha/one",
        "alpha/two",
        "alpha/three",
        "beta/one",
        "beta/two",
        "gamma/one",
        "gamma/two",
    ]
    assert select_review_cases(_CORPUS, limit=0) == []


def _write_report(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps({"report": {"cases": rows}, "cases": rows}), encoding="utf-8"
    )
    return path


def test_failed_case_ids_from_reports_reads_flat_and_nested_shapes(
    tmp_path: Path,
) -> None:
    p1 = _write_report(
        tmp_path,
        "20261101T000000Z.json",
        [
            {"case_id": "memory/note-v1", "condition": "discovery", "status": "fail"},
            {"case_id": "paper/abstract-v2", "condition": "supplied", "status": "pass"},
            {"case_id": "pcb/stack-v1", "condition": "discovery", "status": "error"},
        ],
    )
    p2 = tmp_path / "flat.json"
    p2.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "todo/queue-v1",
                        "condition": "discovery",
                        "status": "fail",
                    },
                    {"case_id": "todo/queue-v2", "status": "skip"},
                ]
            }
        ),
        encoding="utf-8",
    )
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    assert failed_case_ids_from_reports([p1, p2, broken]) == {
        "memory/note-v1",
        "pcb/stack-v1",
        "todo/queue-v1",
    }


def test_latest_stamp_reads_the_newest_dated_line(tmp_path: Path) -> None:
    log = tmp_path / "skill-discovery-review.log"
    assert latest_stamp(log) is None
    log.write_text(
        "**2026-10-01** — first pass\n**2026-11-02** — second pass\n", encoding="utf-8"
    )
    assert latest_stamp(log) == "2026-11-02"


def _rows(supplied: CaseResult, discovery: CaseResult) -> list[CaseResult]:
    return [supplied, discovery]


def test_repair_briefs_one_per_failed_pair_with_direction() -> None:
    def row(status: str, condition: str, **kw: Any) -> CaseResult:
        return CaseResult(
            "memory/note-v1",
            "precis-memory-help",
            status,
            condition=condition,
            **kw,
        )

    retrieval = _rows(
        row("pass", "supplied"),
        row(
            "fail",
            "discovery",
            reasons=("expected call not made: put(kind='memory'",),
            discovery_calls=4,
            discovery_bytes=8192,
        ),
    )
    briefs = repair_briefs(retrieval)
    assert len(briefs) == 1
    assert briefs[0].startswith("[retrieval] memory/note-v1")
    assert "4 calls / 8192 result bytes" in briefs[0]
    assert "search bits / entry points for precis-memory-help" in briefs[0]

    # A passing pair and a half-pair (skipped condition) brief nothing.
    passing = _rows(row("pass", "supplied"), row("pass", "discovery"))
    half = [row("pass", "supplied")]
    assert repair_briefs(passing) == []
    assert repair_briefs(half) == []


def test_latest_discovery_pass_clears_old_failure(tmp_path: Path) -> None:
    old = _write_report(
        tmp_path,
        "01.json",
        [
            {"case_id": "a", "condition": "discovery", "status": "fail"},
            {"case_id": "b", "condition": "supplied", "status": "fail"},
        ],
    )
    new = _write_report(
        tmp_path,
        "02.json",
        [
            {"case_id": "a", "condition": "discovery", "status": "pass"},
            {"case_id": "a", "condition": "supplied", "status": "fail"},
        ],
    )
    assert failed_case_ids_from_reports([new, old]) == set()


def test_nearby_is_bounded_and_retains_effective_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.skill_eval import discovery_review as review
    from precis.skill_eval.runners import ExecutedClaudeRunner

    _write_report(
        tmp_path,
        "01.json",
        [
            {"case_id": "alpha/one", "condition": "discovery", "status": "fail"},
        ],
    )
    monkeypatch.setattr(review, "load_discovery_cases", lambda _: _CORPUS)
    monkeypatch.setattr(ExecutedClaudeRunner, "unavailable_reason", lambda _: None)
    monkeypatch.setattr(ExecutedClaudeRunner, "run", lambda _, case, prompt: case.fake)
    # run_cases binds its skill reader at definition time; use real shipped slugs.
    corpus = review.load_discovery_cases(None)
    from dataclasses import replace

    monkeypatch.setattr(
        review,
        "load_discovery_cases",
        lambda _: [replace(c, skills=("precis-overview",)) for c in corpus],
    )
    assert (
        review.main(["--rerun-nearby", "--limit", "1", "--out-dir", str(tmp_path)]) == 0
    )
    payload = json.loads(
        sorted(tmp_path.glob("*.json"))[-1].read_text(encoding="utf-8")
    )
    assert len(payload["report"]["cases"]) == 2
    assert {r["case_id"] for r in payload["report"]["cases"]} == {"alpha/one"}
    defaults = ExecutedClaudeRunner()
    for key in ("case_usd", "run_usd", "timeout_s", "max_turns"):
        assert payload["budgets"][key] == getattr(defaults, key)
