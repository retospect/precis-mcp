"""A tick raises hypotheses as hypothesis findings serving the quest
(gr674774, me674763): finding minted, ``serves`` edge, ``raised fi<n>``
pointer in the logbook, a gate-failing item skipped with a note."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.hypotheses import serving_hypotheses
from precis.quest.tick import run_quest_tick
from tests.workers._helpers import seed_ref

_SENTENCE = "Nanoindentation measures a modulus above 9 GPa in nanobud films."


def _mk_quest(store: Any) -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text="A striving")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _dispatch(payload: dict[str, Any]) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(
            data=payload, text="", error=None, cost_usd=0.01, paused=False
        )

    return _d


def _log_text(store: Any, qid: int) -> str:
    return "\n".join(c.text for c in store.chunks.list_chunks_for_ref(qid))


def test_tick_mints_serving_finding_and_pointer(store: Any) -> None:
    qid = _mk_quest(store)
    pw1 = seed_ref(store, title="Pathway A", kind="pathway")
    pw2 = seed_ref(store, title="Pathway B", kind="pathway")
    todo = seed_ref(store, title="a direction", kind="todo")
    good = {
        "sentence": _SENTENCE,
        "motivation": "leap",
        "testable_by": "indentation sweep",
        "motivated_by": [f"pw{pw1}", f"pw{pw2}"],
        "direction": f"td{todo}",
    }
    bad = {
        "sentence": "Another claim measures nothing here.",
        "motivation": "leap",
        "testable_by": "x",
        "motivated_by": [f"pw{pw1}"],
    }
    payload = {"logbook": [], "hypotheses": [bad, good]}

    out = run_quest_tick(store, qid, dispatch_fn=_dispatch(payload))
    assert out.status == "succeeded"

    (live,) = serving_hypotheses(store, qid)
    assert live.sentence == _SENTENCE
    outs = store.links_for(live.ref_id, direction="out", relation="serves")
    assert {ln.dst_ref_id for ln in outs} == {qid, todo}
    log = _log_text(store, qid)
    assert f"raised fi{live.ref_id}" in log
    assert "hypothesis not minted" in log

    # re-raising the same sentence converges: no second finding or pointer
    run_quest_tick(store, qid, dispatch_fn=_dispatch(payload))
    assert len(serving_hypotheses(store, qid)) == 1
    assert _log_text(store, qid).count(f"raised fi{live.ref_id}") == 1
