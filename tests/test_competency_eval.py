"""Quest metadata validation and competency answer scoring."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from precis.mesh_eval.competency import (
    CompetencyQuestion,
    answer_handles,
    draft,
    load_questions,
    report,
    run_competency,
    score,
)
from precis.utils import handle_registry
from precis.utils.handle_registry import format_handle
from precis.workers.scheduler import CADENCES

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref


def test_invalid_handle_reported_not_fatal(store: Store) -> None:
    finding = store.insert_ref(kind="finding", slug=None, title="Synthetic finding")
    handle = format_handle(finding.kind, finding.id)
    other_quest = store.insert_ref(kind="quest", slug=None, title="Unrelated quest")
    quest_handle = format_handle(other_quest.kind, other_quest.id)
    quest = store.insert_ref(
        kind="quest",
        slug=None,
        title="Synthetic question set",
        meta={
            "competency_questions": [
                {"question": "Malformed handle?", "expected": ["invalid-handle"]},
                {"question": "Which finding?", "expected": [handle]},
                {
                    "question": "Wrong kind?",
                    "expected": [handle],
                    "forbidden": [quest_handle],
                },
                {"question": "Malformed list?", "expected": handle},
                {"question": "Which measure?", "expected": ["mx12"]},
            ]
        },
    )

    questions, problems = load_questions(store, quest)

    assert questions == [
        CompetencyQuestion("Which finding?", (handle,)),
        CompetencyQuestion("Which measure?", ("mx12",)),
    ]
    assert len(problems) == 3
    assert "row 1" in problems[0] and "invalid-handle" in problems[0]
    assert "row 3" in problems[1] and quest_handle in problems[1]
    assert "row 4" in problems[2] and "expected" in problems[2]


def test_forbidden_hit_fails() -> None:
    question = CompetencyQuestion("Which finding?", ("fi1",), ("fi2",))
    assert score(question, ["fi2"], ["fi1"]) == "fail"


def test_missing_expected_warns() -> None:
    question = CompetencyQuestion("Which records?", ("fi1", "pa2"))
    assert score(question, ["fi1"], ["fi1", "pa3"]) == "warn"
    assert score(question, ["fi1", "pa2", "pa3"], []) == "pass"


def test_found_yesterday_gone_today_is_regressed() -> None:
    question = CompetencyQuestion("Which records?", ("fi1", "pa2"))
    assert score(question, ["fi1"], ["fi1", "pa2"]) == "regressed"


def _card(store: Store, kind: str, text: str) -> str:
    slug = f"syn-{uuid4().hex[:10]}" if kind != "finding" else None
    ref = store.insert_ref(kind=kind, slug=slug, title=text[:20])
    store.chunks.upsert_card_combined(ref.id, text)
    return format_handle(kind, ref.id)


def _quest(store: Store, rows: list[dict[str, object]]) -> Ref:
    return store.insert_ref(
        kind="quest",
        slug=None,
        title="Synthetic set",
        meta={"competency_questions": rows},
    )


def test_each_kind_is_reachable_by_search(store: Store) -> None:
    tag = uuid4().hex[:10]
    handles = {
        kind: _card(store, kind, f"zorblax{tag} {kind} synthetic")
        for kind in ("finding", "material", "paper")
    }
    q = CompetencyQuestion(f"zorblax{tag}", ())
    got = answer_handles(store, q, 20, None)
    assert set(handles.values()) <= set(got)
    assert len(got) == len(set(got))


def test_deliberately_dropped_expected_handle_raises_alert(store: Store) -> None:
    tag = uuid4().hex[:10]
    keep = _card(store, "finding", f"quibblex{tag} keep")
    drop = _card(store, "paper", f"quibblex{tag} drop")
    quest = _quest(store, [{"question": f"quibblex{tag}", "expected": [keep, drop]}])
    assert [r.status for r in run_competency(store, quest)] == ["pass"]
    assert _open_alerts(store, quest) == 0

    parsed = handle_registry.parse(drop)
    assert parsed is not None
    store.retire_ref(parsed[2])
    fresh = store.get_ref(kind="quest", id=quest.id)
    assert fresh is not None
    assert [r.status for r in run_competency(store, fresh)] == ["regressed"]
    assert _open_alerts(store, quest) == 1


def test_unchanged_rerun_raises_nothing_new(store: Store) -> None:
    tag = uuid4().hex[:10]
    keep = _card(store, "finding", f"wuzzlex{tag} keep")
    other = _card(store, "paper", f"wuzzlex{tag} other")
    quest = _quest(
        store,
        [{"question": f"wuzzlex{tag}", "expected": [keep], "forbidden": [other]}],
    )
    assert [r.status for r in run_competency(store, quest)] == ["fail"]
    first = _open_alerts(store, quest)
    fresh = store.get_ref(kind="quest", id=quest.id)
    assert fresh is not None
    run_competency(store, fresh)
    assert first == 1
    assert _open_alerts(store, quest) == first


def _open_alerts(store: Store, quest: Ref) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'alert' AND retired_at IS NULL "
            "AND COALESCE(alert_source, meta->>'alert_source') = 'competency' "
            "AND COALESCE(fingerprint, meta->>'fingerprint') LIKE %s "
            "AND resolved_at IS NULL",
            (f"%:{format_handle('quest', quest.id)}",),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_cadence_runs_every_competency_set(store: Store) -> None:
    tag = uuid4().hex[:10]
    a = _card(store, "finding", f"snazzlex{tag} one")
    quests = [
        _quest(store, [{"question": f"snazzlex{tag}", "expected": [a]}])
        for _ in range(2)
    ]
    assert {c.name for c in CADENCES} >= {"competency_check"}
    cadence = next(c for c in CADENCES if c.name == "competency_check")
    assert cadence.spends is False
    cadence.run(store, 1)
    for quest in quests:
        fresh = store.get_ref(kind="quest", id=quest.id)
        assert fresh is not None
        assert fresh.meta["competency_last"]["status"] == {f"snazzlex{tag}": "pass"}


def test_cli_table_lists_each_question_status(store: Store) -> None:
    tag = uuid4().hex[:10]
    a = _card(store, "finding", f"blorptex{tag} one")
    quest = _quest(
        store,
        [
            {"question": f"blorptex{tag}", "expected": [a]},
            {"question": f"absentxx{tag}", "expected": [a]},
        ],
    )
    handle = format_handle("quest", quest.id)
    table = report(store, handle)
    assert f"pass      blorptex{tag}" in table
    assert f"warn      absentxx{tag}" in table
    assert '"status": "warn"' in report(store, handle, as_json=True)
    assert a in draft(store, handle)
