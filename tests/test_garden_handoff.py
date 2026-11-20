"""Bounded research handoffs for unresolved grounding-audit claims (gr634487).

Dedup is content-pinned, budgets are cumulative and bounded, human-only
gaps route to an ask-user todo with nothing sent outward, and returned
findings are candidate evidence — they never touch the memory, the audit
or the sha pin.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.graph.garden import grounding as g
from precis.graph.garden import handoff as h
from precis.graph.garden.runner import run_garden_tick
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import ChunkInsert, Ref, Tag

S1 = "The ingest worker stores every body chunk before the embedder runs on them (pc1)."
S2 = "The embedder always finishes within one second for every stored document."
S3 = "The dispatcher drops every job whose queue depth exceeds forty."
S4 = "A nightly sweep retires memories nobody has touched in a year."
S5 = "Every review row carries the model name that wrote it."
BODY = f"{S1} {S2}"
BODY5 = f"{S1} {S2} {S3} {S4} {S5}"
_SENTENCES = (S1, S2, S3, S4, S5)


def _memory(store: Store, body: str = BODY, title: str = "Handoff fixture") -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    store.add_tag(ref.id, Tag.open("section:reference"), set_by="system")
    return int(ref.id)


def _reply(*entries: dict[str, Any]) -> g.Call:
    def call(prompt: str) -> tuple[str, str | None]:
        return json.dumps({"claims": list(entries)}), None

    return call


def _unres(i: int, **kw: Any) -> dict[str, Any]:
    """An unresolved claim the classifier could not support — ``evidence``
    missing check by default."""
    base: dict[str, Any] = {
        "i": i,
        "quote": _SENTENCES[i],
        "kind": "empirical",
        "status": "unsupported",
        "evidence": [],
    }
    return {**base, **kw}


def _audit(
    store: Store, rid: int, body: str = BODY, *entries: dict[str, Any]
) -> g.AuditRecord:
    sha = store.target_sha("ref", rid)
    assert sha is not None
    return g.audit_body(body, "Handoff fixture", sha, call=_reply(*entries))


def _handoffs(store: Store) -> list[Ref]:
    return store.list_refs(kind="todo", tags=[h.HANDOFF_TAG], limit=50)


def test_same_key_reuses_existing_todo(store: Store) -> None:
    rid = _memory(store)
    rec = _audit(store, rid, BODY, _unres(0), _unres(1))
    claim = rec.claims[0]
    assert claim.status == "unresolved" and claim.missing_check == "evidence"

    first = h.open_handoff(store, rec, claim, ref_id=rid)
    assert first.status == "minted" and first.todo_id is not None
    assert first.attempts == 1
    assert first.checks == (
        f"Grounding classifier inspected span {claim.span}: {claim.reason}",
    )
    assert h.find_existing(store, first.key) == first.todo_id

    second = h.open_handoff(store, rec, claim, ref_id=rid)
    assert second.status == "reused" and second.todo_id == first.todo_id
    assert second.attempts == 2
    assert [t.id for t in _handoffs(store)] == [first.todo_id]


def test_changed_content_makes_new_key() -> None:
    base = h.handoff_key("shaA", S1, "evidence")
    assert base != h.handoff_key("shaB", S1, "evidence")
    assert base == h.handoff_key("shaA", f"  {S1}\n", "evidence")
    assert base != h.handoff_key("shaA", S2, "evidence")
    assert base != h.handoff_key("shaA", S1, "ambiguous_term")


def test_second_audit_unchanged_creates_no_second_todo(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _memory(store)
    monkeypatch.setattr(g, "_routed_call", lambda ref_id: _reply(_unres(0), _unres(1)))
    tick = run_garden_tick(store, passes=("memory-grounding",))
    assert tick.handoffs == 2
    assert len(_handoffs(store)) == 2

    tick = run_garden_tick(store, passes=("memory-grounding",))
    assert tick.handoffs == 0
    todos = _handoffs(store)
    assert len(todos) == 2
    assert all(t.meta[h.META_KEY]["budget"]["attempts"] == 1 for t in todos)


def test_attempts_exhausted_returns_unresolved_with_checks_listed(
    store: Store,
) -> None:
    rid = _memory(store)
    rec = _audit(store, rid, BODY, _unres(0), _unres(1))
    claim = rec.claims[0]

    results = [
        h.open_handoff(store, rec, claim, ref_id=rid) for _ in range(h.MAX_ATTEMPTS + 1)
    ]
    assert results[0].status == "minted"
    assert results[-1].status == "exhausted"
    assert results[-1].attempts == h.MAX_ATTEMPTS
    assert results[-1].checks == (
        f"Grounding classifier inspected span {claim.span}: {claim.reason}",
    )
    todo_id = results[-1].todo_id
    assert todo_id is not None and store.has_tag(todo_id, "STATUS", "won't-do")
    todo = store.get_ref(kind="todo", id=todo_id)
    assert todo is not None and todo.meta[h.META_KEY]["outcome"] == "exhausted"
    # The claim stays unresolved — exhaustion is a report, not a resolution.
    assert claim.status == "unresolved"
    assert len(_handoffs(store)) == 1


def test_no_email_or_outward_call_made(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambiguous-term gaps mint an ask-user todo and send nothing outward."""
    from precis.utils.llm import router

    def boom(request: Any) -> Any:
        raise AssertionError(f"outward/paid call attempted: {request!r}")

    monkeypatch.setattr(router, "route", boom)
    rid = _memory(store)
    flagged = _unres(0, term_flags=["frobnicator coined as established"])
    rec = _audit(store, rid, BODY, flagged, _unres(1))
    claim = rec.claims[0]
    assert claim.status == "unresolved"
    assert claim.missing_check == "ambiguous_term"

    result = h.open_handoff(store, rec, claim, ref_id=rid)
    assert result.status == "asked" and result.todo_id is not None
    todo = store.get_ref(kind="todo", id=result.todo_id)
    assert todo is not None
    assert (
        isinstance(todo.meta.get("ask"), dict) and todo.meta["ask"]["kind"] == "ruling"
    )
    assert store.has_tag(todo.id, "OPEN", "ask-user")
    assert store.has_tag(todo.id, "OPEN", h.HANDOFF_TAG)
    assert store.has_tag(todo.id, "STATUS", "open")
    # The ask is the only artifact — no job, no second todo, no dispatch.
    assert store.list_refs(kind="job", limit=10) == []
    assert len(_handoffs(store)) == 1


def test_per_tick_cap(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(_SENTENCES) > h.MAX_HANDOFFS_PER_TICK
    rid = _memory(store, body=BODY5)
    prompts: list[str] = []

    def call(prompt: str) -> tuple[str, str | None]:
        prompts.append(prompt)
        return _reply(*(_unres(i) for i in range(5)))(prompt)

    monkeypatch.setattr(g, "_routed_call", lambda ref_id: call)
    tick = run_garden_tick(store, passes=("memory-grounding",))
    assert tick.handoffs == h.MAX_HANDOFFS_PER_TICK
    first = g.latest_audit(store, rid)
    tick = run_garden_tick(store, passes=("memory-grounding",))
    assert tick.handoffs == len(_SENTENCES) - h.MAX_HANDOFFS_PER_TICK
    assert len(_handoffs(store)) == len(_SENTENCES)
    assert len(prompts) == 1 and g.latest_audit(store, rid) == first


def test_failed_write_retries_saved_audit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _memory(store)
    monkeypatch.setattr(g, "_routed_call", lambda ref_id: _reply(_unres(0), _unres(1)))
    write = h._file_todo

    def fail_once(*args: Any, **kwargs: Any) -> int:
        todo_id = write(*args, **kwargs)
        monkeypatch.setattr(h, "_file_todo", write)
        raise RuntimeError(f"simulated failure after minting {todo_id}")

    monkeypatch.setattr(h, "_file_todo", fail_once)
    first = run_garden_tick(store, passes=("memory-grounding",))
    assert first.proposals == 1 and len(_handoffs(store)) == 1
    second = run_garden_tick(store, passes=("memory-grounding",))
    assert second.proposals == 0 and second.handoffs == 1
    assert len(_handoffs(store)) == 2


def test_existing_pinned_question_is_reused_without_swallowing_human_check(
    store: Store,
) -> None:
    rid = _memory(store)
    rec = _audit(store, rid, BODY, _unres(0), _unres(1))
    claim = rec.claims[0]
    question = store.insert_ref(
        kind="todo",
        slug=None,
        title="Investigate ingest ordering",
        meta={
            "research_question": {
                "claim_pins": {
                    "ref": f"me{rid}",
                    "span": claim.span,
                    "sha": rec.target_sha,
                },
                "source_pins": [],
                "missing_check": "evidence",
            }
        },
    )
    store.add_tag(question.id, Tag.closed("STATUS", "done"), set_by="system")
    TodoHandler(hub=Hub(store=store)).put(
        id=question.id,
        text="candidate-evidence: no answer.\nChecks performed: inspected the archived report.",
    )
    result = h.open_handoff(store, rec, claim, ref_id=rid)
    assert result.status == "linked" and result.todo_id == question.id

    assert store.has_tag(question.id, "STATUS", "done")
    assert h.candidate_returns(store, rid, rec.target_sha)[0]["checks_performed"] == [
        "inspected the archived report."
    ]
    assert h.open_handoff(store, rec, claim, ref_id=rid).status == "closed"

    ask = replace(claim, missing_check="identity_or_authority")
    routed = h.open_handoff(store, rec, ask, ref_id=rid)
    assert routed.status == "asked" and routed.todo_id != question.id

    repinned = replace(claim, evidence=["pc1"])
    different = h.open_handoff(store, rec, repinned, ref_id=rid)
    assert different.status == "minted" and different.todo_id != question.id
    original = store.get_ref(kind="todo", id=question.id)
    assert original is not None
    assert original.meta[h.META_KEY]["source_pins"] == []


def test_concurrent_replay_commissions_one_todo_and_one_attempt(store: Store) -> None:
    rid = _memory(store)
    rec = _audit(store, rid, BODY, _unres(0), _unres(1))
    assert g.write_audit(store, rid, rec) is not None
    saved = g.latest_audit(store, rid)
    assert saved is not None

    def commission() -> h.HandoffResult:
        return h.open_handoff(store, saved, saved.claims[0], ref_id=rid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(commission)
        second = pool.submit(commission)
        results = [first.result(), second.result()]
    assert {result.status for result in results} == {"minted", "reused"}
    assert {result.attempts for result in results} == {1}
    assert len(_handoffs(store)) == 1


def test_returned_evidence_cannot_bypass_sha_pin_or_review(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _memory(store)
    monkeypatch.setattr(g, "_routed_call", lambda ref_id: _reply(_unres(0), _unres(1)))
    run_garden_tick(store, passes=("memory-grounding",))
    old = g.latest_audit(store, rid)
    assert old is not None and old.claims[0].status == "unresolved"
    todo = next(
        t for t in _handoffs(store) if t.meta[h.META_KEY]["span"] == old.claims[0].span
    )
    handler = TodoHandler(hub=Hub(store=store))
    handler.put(
        id=todo.id,
        text="candidate-evidence: pc123 describes ingest ordering.\n"
        "Checks performed: inspected section 2 of pc123.",
    )
    assert g.latest_audit(store, rid) == old

    prompts: list[str] = []

    def call(prompt: str) -> tuple[str, str | None]:
        prompts.append(prompt)
        return _reply(_unres(0, status="supported", evidence=["pc123"]), _unres(1))(
            prompt
        )

    monkeypatch.setattr(g, "_routed_call", lambda ref_id: call)
    run_garden_tick(store, passes=("memory-grounding",))
    current = g.latest_audit(store, rid)
    assert len(prompts) == 1 and "pc123 describes ingest ordering" in prompts[0]
    assert current is not None and current.review_id != old.review_id
    assert current.target_sha == old.target_sha == store.target_sha("ref", rid)
    assert current.claims[0].evidence == ["pc123"]
    assert current.claims[0].status == "provisional"
    assert current.coverage["all_grounded"] is False
    assert current.coverage["candidate_returns"][0]["checks_performed"] == [
        "inspected section 2 of pc123."
    ]
    assert [c.text for c in store.chunks.list_chunks_for_ref(rid)] == [BODY]
    assert all(r.verdict == "proposed" for r in store.reviews_for("ref", rid))
    assert g.grounding_audit_candidates(store) == []

    handler.put(
        id=todo.id, text="candidate-evidence: stale return from the old revision."
    )
    store.chunks.insert_chunks(
        rid, [ChunkInsert(ord=1, text=S3, meta={"chunk_kind": "memory_body"})]
    )
    run_garden_tick(store, passes=("memory-grounding",))
    assert len(prompts) == 2 and "stale return from the old revision" not in prompts[-1]
    revised = g.latest_audit(store, rid)
    assert revised is not None and revised.target_sha != old.target_sha
    assert revised.coverage.get("candidate_returns") is None
