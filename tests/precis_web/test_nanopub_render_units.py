"""Pure-shape units for the claim page's maturity ladder + gates report
(``precis_web.nanopub_render._ladder`` / ``_gate_report``) and the ask-box
model label — written to kill the 2026-08-27 mutation survivors: the
route-level tests render the HTML but never asserted which rung is
current/done, which group a status came from, or the label fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from precis_web.nanopub_render import _gate_report, _ladder


@dataclass
class _Row:
    updated_at: datetime | None


@dataclass
class _Issue:
    check: str
    message: str
    blocking: bool = True


def test_ladder_unminted_lights_nothing() -> None:
    steps = _ladder(None, None, disputed=False)
    assert [s["name"] for s in steps] == [
        "candidate",
        "reviewed",
        "signed",
        "anchored",
        "published",
    ]
    assert not any(s["done"] for s in steps)
    assert not any(s["current"] for s in steps)
    assert not any(s["blocked"] for s in steps)


def test_ladder_reviewed_marks_climbed_rungs_and_one_current() -> None:
    row = _Row(updated_at=datetime(2026, 8, 27, 10, 30, tzinfo=UTC))
    steps = {s["name"]: s for s in _ladder("reviewed", row, disputed=False)}
    assert steps["candidate"]["done"] and not steps["candidate"]["current"]
    assert steps["reviewed"]["done"] and steps["reviewed"]["current"]
    assert not steps["signed"]["done"] and not steps["signed"]["current"]
    assert not steps["published"]["done"]
    # The since-timestamp rides ONLY the current rung's tip.
    assert "In this state since 2026-08-27 10:30Z" in steps["reviewed"]["tip"]
    assert not any(
        "In this state since" in s["tip"] for n, s in steps.items() if n != "reviewed"
    )
    assert not any(s["blocked"] for s in steps.values())


def test_ladder_dispute_blocks_only_the_current_rung() -> None:
    steps = {s["name"]: s for s in _ladder("signed", _Row(None), disputed=True)}
    assert steps["signed"]["blocked"]
    assert "BLOCKED" in steps["signed"]["tip"]
    assert not any(s["blocked"] for n, s in steps.items() if n != "signed")


def test_gate_report_unminted_is_all_pending() -> None:
    report = _gate_report(None, [])
    assert report["mint"] and report["preflight"]
    assert all(g["status"] == "pending" for g in report["mint"])
    assert all(g["status"] == "pending" for g in report["preflight"])


def test_gate_report_candidate_mixes_live_issues_with_pending() -> None:
    report = _gate_report("candidate", [_Issue("state", "not anchored", blocking=True)])
    pre = {g["name"]: g for g in report["preflight"]}
    assert all(g["status"] == "pending" for g in report["mint"])
    assert pre["state"]["status"] == "failed"
    assert pre["state"]["message"] == "not anchored"
    assert pre["withheld-edge"]["status"] == "pending"


def test_gate_report_reviewed_mint_passed_preflight_reads_live_issues() -> None:
    report = _gate_report(
        "reviewed",
        [
            _Issue("state", "state is 'reviewed', not 'anchored'", blocking=True),
            _Issue("ots-pending", "calendar-pending", blocking=False),
        ],
    )
    # Approve refuses on any mint-gate violation, so state ≥ reviewed
    # means every mint gate passed — mechanically, all of them.
    assert all(g["status"] == "passed" for g in report["mint"])
    pre = {g["name"]: g for g in report["preflight"]}
    assert pre["state"]["status"] == "failed"
    assert pre["ots-pending"]["status"] == "note"
    # A check with no live issue at reviewed+ reads passed, not pending.
    assert pre["withheld-edge"]["status"] == "passed"
    assert pre["trust"]["status"] == "passed"


def test_answer_model_label_env_chain(monkeypatch) -> None:
    from precis_web.ask import answer_model_label

    monkeypatch.delenv("PRECIS_FOLLOWUP_MODEL", raising=False)
    monkeypatch.delenv("PRECIS_DREAM_AGENT_MODEL", raising=False)
    assert answer_model_label() == "sonnet"
    monkeypatch.setenv("PRECIS_DREAM_AGENT_MODEL", "haiku")
    assert answer_model_label() == "haiku"
    monkeypatch.setenv("PRECIS_FOLLOWUP_MODEL", "opus")
    assert answer_model_label() == "opus"
