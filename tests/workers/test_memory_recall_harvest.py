"""Recall harvest uses fixture traces and the test DB, never prod telemetry."""

from __future__ import annotations

from typing import Any

import pytest

from precis.cli.memory_recall_set import FIELDS, PROPOSALS_KEY
from precis.embedder import MockEmbedder
from precis.store import Tag
from precis.workers import memory_recall_harvest as harvest
from precis.workers.scheduler import CADENCES
from precis.workers.service_config import set_service_prio


class QueryEmbedder(MockEmbedder):
    def embed_one(self, text: str) -> list[float]:
        if text in {"known wording", "same meaning", "another paraphrase"}:
            return [1.0] + [0.0] * (self.dim - 1)
        return super().embed_one(text)


def _memory(store, *, space="repo-dev") -> str:
    ref = store.insert_ref(
        kind="memory",
        slug=None,
        title="An observed answer",
        meta={"hook": "Answer hook"},
    )
    store.add_tag(ref.id, Tag.closed("SPACE", space), set_by="system")
    return f"me{ref.id}"


def _trace(
    store,
    q: str | None,
    handles: list[str],
    *,
    used: list[str] | None = None,
    followed: list[str] | None = None,
) -> int:
    return store.insert_ref(
        kind="agentlog",
        slug=None,
        title="Fixture search trace",
        meta={
            "trace": "search",
            "q": q,
            "kinds": ["memory"],
            "mode": "hybrid",
            "session": 123,
            "candidates": [{"handle": h, "rendered": True} for h in handles],
            "used": used or [],
            "followed": followed or [],
        },
    ).id


def _run(store, **kwargs):
    return harvest.run_memory_recall_harvest_pass(
        store, entries=kwargs.pop("entries", []), embedder=QueryEmbedder(), **kwargs
    )


def _batch(store, todo_id) -> list[dict[str, Any]]:
    ref = store.get_ref(kind="todo", id=todo_id)
    assert "llm_tier" not in ref.meta
    return ref.meta[PROPOSALS_KEY]


def test_dark_gate_does_not_read_traces_or_embed(store, monkeypatch):
    def unexpected(*a, **k):
        pytest.fail("dark harvest must not read traces or embed")

    monkeypatch.setattr(harvest, "propose_pairs", unexpected)
    assert _run(store).claimed == 0
    assert not harvest.harvest_enabled(store)
    set_service_prio(store, "*", harvest.SERVICE_NAME, 0, actor="test")
    assert _run(store).claimed == 0
    set_service_prio(store, "*", harvest.SERVICE_NAME, 1, actor="test")
    assert harvest.harvest_enabled(store)


def test_weekly_cadence_calls_dark_pass(store):
    cadence = next(c for c in CADENCES if c.name == harvest.SERVICE_NAME)
    assert 7 * 24 * 3600 <= cadence.interval_s < 8 * 24 * 3600
    assert cadence.eligible is not None
    assert not cadence.spends
    cadence.run(store, 32)
    assert store.list_refs(kind="todo") == []


def test_fixture_positives_become_one_parked_batch_and_do_not_repeat(store):
    handle = _memory(store)
    used = _trace(store, "first observed query", [handle], used=[handle])
    followed = _trace(store, "another question", [handle], followed=[handle])
    _trace(store, "unlabeled only", [handle])
    _trace(store, " \n", [handle], used=[handle])
    _trace(store, None, [handle], followed=[handle])
    set_service_prio(store, "*", harvest.SERVICE_NAME, 1, actor="test")
    result = _run(store)
    assert result.claimed == result.ok == 2
    pairs = _batch(store, result.extra["todo_id"])
    assert [p["query"] for p in pairs] == ["first observed query", "another question"]
    assert all(p.keys() == FIELDS and p["source"] == "trace" for p in pairs)
    assert all(p["title"] == "An observed answer" for p in pairs)
    assert all(p["hook"] == "Answer hook" for p in pairs)
    assert f"ag{used}: used {handle}" in pairs[0]["why"]
    assert f"ag{followed}: followed {handle}" in pairs[1]["why"]
    body = "\n".join(
        c.text for c in store.chunks.list_chunks_for_ref(result.extra["todo_id"])
    )
    assert "precis memory recall-set accept" in body
    assert "1. An observed answer" in body and "2. An observed answer" in body
    assert "why:" in body and "--only 1,3" in body
    assert _run(store).ok == 0
    assert len(store.list_refs(kind="todo")) == 1
    # Settled review history is still evidence, not a fresh batch next week.
    store.add_tag(
        result.extra["todo_id"], Tag.closed("STATUS", "done"), set_by="system"
    )
    assert _run(store).ok == 0


def test_semantic_dedup_against_existing_set(store):
    handle = _memory(store)
    _trace(store, "same meaning", [handle], used=[handle])
    _trace(store, "new observed question", [handle], followed=[handle])
    result = _run(store, enabled=True, entries=[{"query": "known wording"}])
    assert result.ok == 1
    assert _batch(store, result.extra["todo_id"])[0]["query"] == "new observed question"


def test_semantic_dedup_within_batch_and_against_prior_batches(store):
    handle = _memory(store)
    _trace(store, "known wording", [handle], used=[handle])
    _trace(store, "same meaning", [handle], used=[handle])
    assert _run(store, enabled=True).ok == 1
    _trace(store, "another paraphrase", [handle], followed=[handle])
    assert _run(store, enabled=True).ok == 0


def test_misses_unscorable_and_retired_answers_are_not_proposed(store):
    good = _memory(store)
    retired = _memory(store)
    store.retire_ref(int(retired[2:]))
    research = _memory(store, space="research")
    explicit = _trace(store, "explicit miss", [good], followed=[good])
    store.add_tag(explicit, Tag.open("trace:miss"), set_by="system")
    todo = store.insert_ref(kind="todo", slug=None, title="review concern")
    _trace(store, "graph miss", [good], used=[good])
    store.add_link(
        src_ref_id=todo.id,
        dst_ref_id=int(good[2:]),
        relation="raises-concern-about",
        set_by="system",
    )
    _trace(store, "retired answer", [retired], followed=[retired])
    _trace(store, "outside scorer scope", [research], used=[research])
    _trace(store, "wrong kind", [f"td{todo.id}"], followed=[f"td{todo.id}"])
    assert _run(store, enabled=True).ok == 0


def test_used_answer_preferred_and_multiple_positives_keep_one_query(store):
    followed, used = _memory(store), _memory(store)
    ident = _trace(
        store, "which answer", [followed, used], used=[used], followed=[followed, used]
    )
    result = _run(store, enabled=True)
    assert result.ok == 1
    pair = _batch(store, result.extra["todo_id"])[0]
    assert pair["want"] == used
    assert f"ag{ident}: used, followed {used}" in pair["why"]


def test_cap_is_fifty_and_next_run_only_proposes_remainder(store):
    handle = _memory(store)
    for n in range(52):
        _trace(store, f"distinct fixture query {n}", [handle], used=[handle])
    first = _run(store, enabled=True)
    second = _run(store, enabled=True)
    assert first.ok == 50
    assert second.ok == 2
    assert _run(store, enabled=True).ok == 0
