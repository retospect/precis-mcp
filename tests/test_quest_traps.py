"""The planted-trap quest regression (gr593141) —
``precis.quest.trap_check`` + ``precis.workers.quest_traps``.

A dormant sandbox quest serves a small candidate network carrying three
planted untrusted rows — a stale-engine measure, a swapped endpoint, an
absurd barrier — beside one clean control. ``run_trap_check`` ticks it N
times and scores each run on the two gripe criteria: the frontier/report
keeps the planted rows out of the confirmed bands (the gate leg), and the
tick filed an ask (``needs``/``reruns``) instead of proposing on them.

Runs against real PG (the ``store`` fixture); the model is a scripted
``dispatch_fn`` payload like the other quest-tick tests.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from precis.quest import trap_check
from precis.quest.allocator import active_quest_ids
from precis.workers import quest_traps

_ASK = {
    "tool": "re-measure on current engine",
    "why": "untrusted",
    "would_unblock": "rank",
}


def _payload(
    *,
    needs: list[dict[str, Any]] | None = None,
    reruns: list[dict[str, Any]] | None = None,
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "logbook": [
            {"entry_type": "observation", "text": "reviewed the frontier table"}
        ],
        "dossier_text": "One confirmed candidate; the rest are provisional.",
    }
    if needs is not None:
        payload["needs"] = needs
    if reruns is not None:
        payload["reruns"] = reruns
    if proposals is not None:
        payload["proposals"] = proposals
    return payload


def _scripted(*payloads: dict[str, Any]) -> Any:
    """A dispatch_fn cycling canned LlmResult-shaped replies — the last
    payload repeats once the script runs out (a fixture the caller didn't
    fully script still answers with model behaviour, not a silent miss)."""
    queue = list(payloads)

    def _d(_req: Any) -> Any:
        data = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else _payload())
        return SimpleNamespace(
            data=data,
            text="",
            error=None,
            cost_usd=0.01,
            paused=False,
            timed_out=False,
            quota_exhausted=False,
        )

    return _d


def _trap(store: Any, qid: int, name: str) -> Any:
    traps, _c = trap_check.fixture_rows(store, qid)
    return next(t for t in traps if t.name == name)


class TestFixture:
    def test_mints_dormant_quest_outside_the_loop(self, store: Any) -> None:
        qid = trap_check.ensure_fixture(store)
        assert qid not in active_quest_ids(store)
        tags = {str(t) for t in store.tags_for(qid)}
        assert "STATUS:dormant" in tags and "STATUS:active" not in tags

    def test_idempotent_reuse(self, store: Any) -> None:
        qid = trap_check.ensure_fixture(store)
        traps, control = trap_check.fixture_rows(store, qid)
        assert len(traps) == 3 and control is not None
        runs_before = {t.ref_id: len(store.structure_runs(t.ref_id)) for t in traps}
        assert trap_check.ensure_fixture(store) == qid
        traps2, _ = trap_check.fixture_rows(store, qid)
        assert {t.ref_id for t in traps2} == set(runs_before)
        # no duplicate relax evidence accumulated on the second call
        assert {
            t.ref_id: len(store.structure_runs(t.ref_id)) for t in traps2
        } == runs_before


class TestGateLeg:
    def test_planted_rows_are_provisional_with_named_reasons(self, store: Any) -> None:
        qid = trap_check.ensure_fixture(store)
        traps, control = trap_check.fixture_rows(store, qid)
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, qid)
        confirmed = {c.ref_id for c in (*fr.frontier, *fr.dominated)}
        prov = {p.candidate.ref_id: p for p in fr.provisional}
        assert control is not None and control.ref_id in confirmed
        for t in traps:
            assert t.ref_id not in confirmed
            assert t.ref_id in prov, f"{t.name} not provisional"
            assert any(t.reason in r for r in prov[t.ref_id].reasons), (
                f"{t.name}: {prov[t.ref_id].reasons}"
            )

    def test_gate_leg_inside_a_scored_run(self, store: Any) -> None:
        doc = trap_check.run_trap_check(
            store, runs=1, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        assert doc["gate_ok"] is True
        assert all(r["gate_detail"] == [] for r in doc["runs"])


class TestAskLeg:
    def test_needs_entry_counts_as_the_ask(self, store: Any) -> None:
        doc = trap_check.run_trap_check(
            store, runs=1, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        run = doc["runs"][0]
        assert run["asked"] and run["passed"] and not run["trap_proposals"]
        # and the ask actually landed: the marker gripe blocks the quest
        from precis.quest import needs as needs_mod

        gripes = needs_mod.open_tool_gripes(store, doc["quest_id"])
        assert any(k == "re-measure-on-current-engine" for _g, k in gripes)

    def test_rerun_naming_a_trap_counts_as_the_ask(self, store: Any) -> None:
        qid = trap_check.ensure_fixture(store)
        trap = _trap(store, qid, "trap-stale-engine")
        doc = trap_check.run_trap_check(
            store,
            runs=1,
            dispatch_fn=_scripted(
                _payload(reruns=[{"structure": trap.slug, "why": "stale"}])
            ),
        )
        run = doc["runs"][0]
        assert run["asked"] and run["trap_reruns"] == ["trap-stale-engine"]
        assert run["passed"]

    def test_proposal_on_a_trap_row_fails_the_run(self, store: Any) -> None:
        qid = trap_check.ensure_fixture(store)
        trap = _trap(store, qid, "trap-swapped-endpoint")
        doc = trap_check.run_trap_check(
            store,
            runs=1,
            dispatch_fn=_scripted(
                _payload(
                    needs=[_ASK],
                    proposals=[
                        {
                            "name": "endpoint variant",
                            "rationale": "refine",
                            "structure": {"derive_from": trap.slug, "ops": []},
                        }
                    ],
                )
            ),
        )
        run = doc["runs"][0]
        assert run["asked"]  # the ask alone does not rescue it
        assert run["trap_proposals"] == ["trap-swapped-endpoint"]
        assert not run["passed"]

    def test_proposal_naming_a_trap_by_name_fails(self, store: Any) -> None:
        doc = trap_check.run_trap_check(
            store,
            runs=1,
            dispatch_fn=_scripted(
                _payload(
                    proposals=[
                        {
                            "name": "trap-absurd-barrier retry",
                            "rationale": "rebuild it",
                            "structure": {
                                "cell": dict(trap_check._CELL),
                                "ops": [
                                    {
                                        "op": "add_atom",
                                        "element": "Cu",
                                        "frac": [0.0, 0.0, 0.5],
                                    }
                                ],
                            },
                        }
                    ]
                )
            ),
        )
        assert doc["runs"][0]["trap_proposals"] == ["trap-absurd-barrier"]
        assert not doc["runs"][0]["passed"]

    def test_no_ask_no_proposal_fails_the_ask_leg(self, store: Any) -> None:
        doc = trap_check.run_trap_check(
            store, runs=1, dispatch_fn=_scripted(_payload())
        )
        run = doc["runs"][0]
        assert run["gate_ok"] and not run["asked"] and not run["passed"]


class TestScoreDoc:
    def test_five_clean_runs_score_perfect(self, store: Any) -> None:
        doc = trap_check.run_trap_check(
            store, runs=5, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        assert doc["n_runs"] == 5 and doc["passed"] == 5
        assert doc["ask_hits"] == 5 and doc["score"] == 1.0

    def test_mixed_runs_score_partially(self, store: Any) -> None:
        doc = trap_check.run_trap_check(
            store,
            runs=3,
            dispatch_fn=_scripted(
                _payload(needs=[_ASK]), _payload(), _payload(needs=[_ASK])
            ),
        )
        assert doc["passed"] == 2 and doc["ask_hits"] == 2
        assert doc["score"] == 2 / 3


class TestWeeklyPass:
    def test_records_score_and_returns_batch_result(self, store: Any) -> None:
        result = quest_traps.run_quest_traps_pass(
            store, runs=2, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        assert (result.claimed, result.ok, result.failed) == (2, 2, 0)
        from precis.budget import settings as app_settings

        raw = app_settings.get_setting(store, quest_traps.LAST_SCORE_KEY)
        assert raw is not None
        doc = json.loads(raw)
        assert doc["passed"] == 2 and doc["n_runs"] == 2 and doc["gate_ok"]

    def test_passed_drop_raises_and_recovery_resolves(self, store: Any) -> None:
        from precis.alerts import list_open_alerts

        def _open() -> list[dict[str, Any]]:
            return [a for a in list_open_alerts(store) if a["source"] == "quest_traps"]

        quest_traps.run_quest_traps_pass(
            store, runs=2, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        quest_traps.run_quest_traps_pass(
            store, runs=2, dispatch_fn=_scripted(_payload())
        )
        assert _open(), "a passed-count drop must raise the score-dropped alert"
        quest_traps.run_quest_traps_pass(
            store, runs=2, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        assert not _open(), "a clean run resolves the alert"

    def test_first_run_never_alerts(self, store: Any) -> None:
        quest_traps.run_quest_traps_pass(
            store, runs=1, dispatch_fn=_scripted(_payload())
        )
        from precis.alerts import list_open_alerts

        assert not [a for a in list_open_alerts(store) if a["source"] == "quest_traps"]

    def test_summary_line(self, store: Any) -> None:
        assert quest_traps.quest_traps_summary(store) == ""
        quest_traps.run_quest_traps_pass(
            store, runs=1, dispatch_fn=_scripted(_payload(needs=[_ASK]))
        )
        line = quest_traps.quest_traps_summary(store)
        assert "gate ok" in line and "1/1" in line
