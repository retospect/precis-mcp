"""gr464538: leaving the loop (inactive / cancelled) between ``apply`` and
``finish`` must still run the tick's finish stage — real PG, real checkpoint.

The RC2 self-rest used to return before routing to the finish stage, so a quest
cooled mid-tick kept its applied ledger chunks but lost ``tick_count``, the
stall clock and the tick-spend deed.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.tick import TickSlice, run_quest_tick
from precis.workers.executors._yield import Done
from precis.workers.job_types import quest_tick as qt

_PAYLOAD: dict[str, Any] = {
    "logbook": [{"entry_type": "observation", "text": "an observation"}],
}


class _Ctx:
    def __init__(self, store: Any, meta: dict[str, Any], *, cancel: bool = False):
        self.store = store
        self.ref_id = 700
        self.meta = meta
        self.chunks: list[tuple[str, str]] = []
        self._cancel = cancel

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def is_cancel_requested(self) -> bool:
        return self._cancel


def _mk_quest(store: Any) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text="A striving")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _disp(_req: Any) -> Any:
    return SimpleNamespace(
        data=_PAYLOAD,
        text="",
        error=None,
        cost_usd=None,
        paused=False,
        timed_out=False,
        quota_exhausted=False,
    )


def _park_after_apply(store: Any, qid: int) -> dict[str, Any]:
    state: dict[str, Any] | None = None
    for _ in range(2):  # llm, apply
        r = run_quest_tick(store, qid, dispatch_fn=_disp, tick_state=state, sliced=True)
        assert isinstance(r, TickSlice)
        state = json.loads(json.dumps(r.state))
    assert state is not None
    assert state["stage"] == "search"
    return state


def _ctx(
    store: Any, qid: int, state: dict[str, Any] | None, *, cancel: bool = False
) -> _Ctx:
    meta: dict[str, Any] = {
        "job_type": "quest_tick",
        "executor": "coordinator",
        "params": {"quest_id": qid, "tier": "big"},
    }
    if state is not None:
        meta["coordinator_state"] = {
            "phase": f"tick:{state['stage']}",
            "slice_count": 3,
            "tick": state,
        }
    return _Ctx(store, meta, cancel=cancel)


def _tick_count(store: Any, qid: int) -> int:
    ref = store.get_ref(kind="quest", id=qid)
    return int((ref.meta or {}).get("tick_count") or 0)


def _spend_deeds(store: Any, qid: int) -> list[Any]:
    return [
        c
        for c in store.chunks.list_chunks_for_ref(qid)
        if (c.meta or {}).get("entry_type") == "cost"
    ]


def test_inactive_mid_tick_runs_finish_then_rests(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _mk_quest(store)
    state = _park_after_apply(store, qid)
    before = _tick_count(store, qid)
    spend_before = len(_spend_deeds(store, qid))
    monkeypatch.setattr(qt, "active_quest_ids", lambda store: [])
    out = qt._dispatch(_ctx(store, qid, state), qt.SPEC)
    assert isinstance(out, Done)
    assert out.summary_meta.get("self_rested") is True
    assert _tick_count(store, qid) == before + 1
    assert len(_spend_deeds(store, qid)) == spend_before + 1


def test_inactive_without_a_tick_in_flight_does_not_bump(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    qid = _mk_quest(store)
    before = _tick_count(store, qid)
    monkeypatch.setattr(qt, "active_quest_ids", lambda store: [])
    out = qt._dispatch(_ctx(store, qid, None), qt.SPEC)
    assert isinstance(out, Done)
    assert out.summary_meta.get("self_rested") is True
    assert _tick_count(store, qid) == before


def test_cancel_mid_tick_still_finishes(store: Any) -> None:
    qid = _mk_quest(store)
    state = _park_after_apply(store, qid)
    before = _tick_count(store, qid)
    out = qt._dispatch(_ctx(store, qid, state, cancel=True), qt.SPEC)
    assert isinstance(out, Done)
    assert out.summary_meta.get("cancelled") is True
    assert _tick_count(store, qid) == before + 1
