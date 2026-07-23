"""Tests for `precis quest status` — the read-only ops roll-up (:mod:`precis.
quest.status`). Covers the five parts: logbook tail, candidates + measures +
ruled-out tags, sim-job status roll, coordinator tick-event trail, per-quest
LLM spend/errors. Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from typing import Any

from precis import route_log
from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import status as status_mod
from precis.route_log import LlmCallRecord
from precis.store import Tag

_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


def _mk_quest(store: Any, text: str) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def test_status_reports_none_for_missing_quest(store: Any) -> None:
    assert status_mod.gather_quest_status(store, 999_999_999) is None


def test_status_degrades_cleanly_for_a_fresh_quest(store: Any) -> None:
    qid = _mk_quest(store, "A fresh striving")
    status = status_mod.gather_quest_status(store, qid)
    assert status is not None
    assert status.logbook_tail == []
    assert status.candidates == []
    assert status.sim_jobs == []
    assert status.tick_events == []
    assert status.llm_spend.calls == 0
    # renders without raising even when everything is empty
    text = status_mod.render_quest_status(status)
    assert "not found" not in text


def test_status_gathers_logbook_candidates_sim_jobs_and_llm_spend(
    store: Any,
) -> None:
    from precis.quest.logbook import append_entry

    qid = _mk_quest(store, "A NO→NH₃ catalyst")
    append_entry(store, qid, text="looked at Pd(111)", entry_type="note", by="agent")
    append_entry(
        store, qid, text="ruled out bare Fe", entry_type="dead-end", by="agent"
    )

    sid = compute_mod.ensure_candidate(store, qid, {"name": "Fe", "structure": _SPEC})
    assert sid is not None
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=12,
        max_disp=0.0,
        energy=-9.5,
    )
    # a struct_relax job under the candidate, tagged failed+ruled-out on the
    # candidate itself (mirrors the real harvest path)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="struct_relax",
        meta={"job_type": "struct_relax", "failure_class": "infra"},
        parent_id=sid,
    )
    store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")

    route_log.record_call(
        LlmCallRecord(
            source="quest_tick",
            tier="cloud-small",
            transport="claude_agent",
            model="claude-x",
            tools_needed=False,
            request_text="req",
            response_text="resp",
            cost_usd=0.02,
            turns_used=1,
            duration_ms=500,
            errored=False,
            error=None,
            data_parsed=None,
            ref_id=qid,
        ),
        store=store,
    )
    route_log.record_call(
        LlmCallRecord(
            source="quest_tick",
            tier="cloud-small",
            transport="claude_agent",
            model="claude-x",
            tools_needed=False,
            request_text="req2",
            response_text="resp2",
            cost_usd=None,
            turns_used=1,
            duration_ms=100,
            errored=True,
            error="rate limited",
            data_parsed=None,
            ref_id=qid,
        ),
        store=store,
    )

    status = status_mod.gather_quest_status(store, qid)
    assert status is not None

    assert len(status.logbook_tail) == 2
    assert status.logbook_tail[0].entry_type == "note"
    assert status.logbook_tail[1].entry_type == "dead-end"

    assert len(status.candidates) == 1
    cand = status.candidates[0]
    assert cand.ref_id == sid
    assert cand.converged is True
    assert cand.measures.get("energy") == -9.5

    assert len(status.sim_jobs) == 1
    sj = status.sim_jobs[0]
    assert sj.job_type == "struct_relax"
    assert sj.status == "failed"

    assert status.llm_spend.calls == 2
    assert status.llm_spend.errors == 1
    assert status.llm_spend.real_usd == 0.02
    assert len(status.llm_spend.recent_errors) == 1
    assert status.llm_spend.recent_errors[0][2] == "rate limited"

    text = status_mod.render_quest_status(status)
    assert "looked at Pd(111)" in text
    assert "struct_relax" in text
    assert "rate limited" in text
    assert "2 call(s)" in text and "1 error(s)" in text


def test_status_tick_events_from_coordinator_job(store: Any) -> None:
    """The autonomous loop's own `quest_tick` coordinator job's `job_event`
    chunks — identified by ``meta.params.quest_id``, not parent_id (the
    coordinator job type isn't yet wired to parent on the quest itself)."""
    from precis.store.types import BlockInsert

    qid = _mk_quest(store, "A striving")
    todo = store.insert_ref(kind="todo", slug=None, title="drive the quest")
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="quest_tick",
        meta={
            "job_type": "quest_tick",
            "executor": "coordinator",
            "params": {"quest_id": qid, "tier": "local-big"},
        },
        parent_id=todo.id,
    )
    with store.tx() as conn:
        store.insert_blocks(
            job.id,
            [
                BlockInsert(
                    pos=0,
                    text="tick 1: proposed 3 candidates",
                    meta={"chunk_kind": "job_event"},
                ),
                BlockInsert(
                    pos=1, text="awaiting sims", meta={"chunk_kind": "job_event"}
                ),
            ],
            conn=conn,
        )

    status = status_mod.gather_quest_status(store, qid)
    assert status is not None
    assert [e.text for e in status.tick_events] == [
        "tick 1: proposed 3 candidates",
        "awaiting sims",
    ]
    assert all(e.job_id == job.id for e in status.tick_events)
