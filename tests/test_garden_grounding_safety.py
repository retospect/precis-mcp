"""Safety rails around the grounding auditor (gr634496): the audit never
touches the body, a stale sha cannot apply, human-origin and DREAM memories
are held, the existing rewrite door round-trips through undo, and a repeat
run mints nothing new. Repairs are scored apart from audit judgments.

Real PostgreSQL fixtures; model replies are injected, no network.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from precis.graph.garden import apply as doors
from precis.graph.garden import candidates as cand
from precis.graph.garden import grounding as g
from precis.graph.garden.proposals import emit_proposal, pending_proposals
from precis.graph.garden.runner import run_garden_tick
from precis.mesh_eval import grounding as ge
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import ChunkInsert, Tag
from precis.utils.handle_registry import try_format

BAD = "The catalyst doubles the yield"
KEPT_A = "Reviewed on the first of the month."
KEPT_B = "The notebook lives in the shared drawer."


def _paper(store: Store) -> str:
    paper = store.insert_ref(
        kind="paper", slug=f"safety-{uuid.uuid4().hex[:12]}", title="Source"
    )
    (chunk,) = store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="The catalyst halves the yield.")]
    )
    handle = try_format("paper", int(chunk.id), chunk=True)
    assert handle is not None
    return handle


def _memory(store: Store, body: str, *, origin: str = "generated") -> int:
    with revision_context(
        "grounding safety fixture",
        actor="fixture",
        trust_origin=origin,
        trust_actor_type="human" if origin == "human" else "agent",
    ):
        ref = store.insert_ref(kind="memory", slug=None, title="Safety fixture")
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
        )
    return int(ref.id)


def _body(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'memory_body' AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _classify(handle: str) -> g.Call:
    def call(_prompt: str) -> tuple[str, str | None]:
        claims: list[dict[str, Any]] = [
            {
                "i": 0,
                "quote": BAD,
                "kind": "empirical",
                "status": "supported",
                "evidence": [handle],
            },
            {"i": 1, "kind": "nonclaim"},
            {"i": 2, "kind": "nonclaim"},
        ]
        return json.dumps({"claims": claims}), None

    return call


def _reject(_claim: Any, _passages: Any) -> g.RawSupport:
    return g.RawSupport(supported=False, reason="passage says the opposite")


def _audited_memory(store: Store) -> tuple[int, str, str]:
    handle = _paper(store)
    body = f"{BAD} ({handle}). {KEPT_A} {KEPT_B}"
    ref_id = _memory(store, body)
    (candidate,) = [
        c for c in g.grounding_audit_candidates(store) if c.target_id == ref_id
    ]
    assert (
        g.audit_candidate(store, candidate, call=_classify(handle), verify_fn=_reject)
        is not None
    )
    return ref_id, body, handle


def _dangling_rewrite(store: Store) -> tuple[int, int, str]:
    """A reviewed dangling-mention rewrite proposal (the existing door)."""
    successor = _memory(store, "Current source.")
    old = _memory(store, "Former source.")
    store.stamp_ref_meta(old, {"superseded_by": successor})
    store.retire_ref(old)
    body = f"See me{old} for the source.  {KEPT_A}\n{KEPT_B}\n"
    target = _memory(store, body)
    candidate = next(
        c for c in cand.dangling_mention_candidates(store) if c.target_id == target
    )
    review_id = emit_proposal(store, candidate, rationale="Checked against source.")
    assert review_id is not None
    return target, review_id, body


def test_preservation_of_untouched_sentences(store: Store) -> None:
    ref_id, body, _handle = _audited_memory(store)
    # The audit only reads: the body is byte-identical afterwards.
    assert _body(store, ref_id) == body
    (proposal,) = pending_proposals(store)
    a, b = proposal.payload["span"]
    # The repair is pinned to the offending claim; the other sentences sit
    # outside its span, so a rewrite of that span cannot reach them.
    assert body[a:b] == BAD
    assert KEPT_A not in body[a:b] and KEPT_B not in body[a:b]
    assert body[:a] + body[b:] == body.replace(BAD, "")
    # The existing rewrite door keeps untouched sentences byte for byte.
    target, review_id, before = _dangling_rewrite(store)
    doors.apply_rewrite(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    after = _body(store, target)
    assert after != before
    assert KEPT_A in after and KEPT_B in after


def test_stale_sha_refused_at_apply(store: Store) -> None:
    ref_id, _body_text, _handle = _audited_memory(store)
    (proposal,) = pending_proposals(store)
    assert proposal.current
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.update_ref(ref_id, title="Edited after the audit")
    # The audit and its repair stop binding once the memory moves …
    summary = g.grounding_summary(store, ref_id)
    assert summary.freshness == "stale"
    assert not next(
        p for p in pending_proposals(store) if p.review_id == proposal.review_id
    ).current
    # … and the existing apply door refuses a stale proposal outright.
    target, review_id, before = _dangling_rewrite(store)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.update_ref(target, title="Changed after proposal")
    with pytest.raises(doors.MergeRefused) as exc:
        doors.apply_rewrite(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert exc.value.reason == "stale"
    assert _body(store, target) == before


def test_human_origin_and_dream_held(store: Store) -> None:
    human = _memory(store, f"{BAD}.", origin="human")
    dream = _memory(store, f"{BAD}.")
    store.add_tag(dream, Tag.closed("DREAM", "fixture"), set_by="system")
    plain = _memory(store, f"{BAD}.")
    ids = {c.target_id for c in g.grounding_audit_candidates(store)}
    assert plain in ids
    assert human not in ids and dream not in ids
    # Even a hand-built candidate for the human memory is refused at audit.
    sha = store.trust_state("ref", human)
    assert sha is not None and sha.sha is not None
    held = cand.GardenCandidate(
        detector="grounding_audit",
        action="audit",
        target_kind="ref",
        target_id=human,
        title="grounding audit",
        evidence={},
        payload={"sha": sha.sha},
    )

    def never(_prompt: str) -> tuple[str, str | None]:
        raise AssertionError("a held memory must never reach the classifier")

    assert g.audit_candidate(store, held, call=never) is None
    assert g.latest_audit(store, human) is None
    assert pending_proposals(store) == []


def test_apply_then_undo_restores_body(store: Store) -> None:
    target, review_id, before = _dangling_rewrite(store)
    rec = doors.apply_rewrite(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    assert _body(store, target) != before
    doors.undo_rewrite(store, rec.rewrite_id, actor="operator")
    assert _body(store, target).encode("utf-8") == before.encode("utf-8")


def test_repeat_run_idempotent_no_new_proposals(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _paper(store)
    ref_id = _memory(store, f"{BAD} ({handle}). {KEPT_A} {KEPT_B}")
    monkeypatch.setattr(g, "_routed_call", lambda _ref_id: _classify(handle))
    monkeypatch.setattr(g, "_reviewer_support", lambda _c, _p: _reject(_c, _p))
    first = run_garden_tick(store, passes=("memory-grounding",), max_proposals=10)
    assert first.proposals >= 1
    pending = {p.key for p in pending_proposals(store)}
    assert pending
    # The audited sha is not audited again: only the documented baseline
    # slot may revisit it, and that revisit mints no repair.
    assert all(c.payload.get("baseline") for c in g.grounding_audit_candidates(store))
    for _ in range(2):
        run_garden_tick(store, passes=("memory-grounding",), max_proposals=10)
        assert {p.key for p in pending_proposals(store)} == pending
    assert g.latest_audit(store, ref_id) is not None


def test_repairs_scored_apart_from_audit_judgments() -> None:
    # aa-01: a weakly evidenced mechanism claim. The audit judges it
    # ``partial`` (correct), yet the current repair policy proposes nothing
    # for a weak-evidence partial — a repair miss that must not leak into
    # the audit-judgment rates.
    case = next(c for c in ge.load_cases() if c.case_id == "aa-01")
    run = ge.run_case(case)
    (outcome,) = ge.score_run(run)
    assert outcome.correct and outcome.stage == ""
    report = ge.build_report([run])
    assert report.metrics["status_accuracy"].k == 1
    repairs = ge.score_repairs([run])
    assert repairs.correct.k == 0 and repairs.correct.n == 1
    assert repairs.rows and repairs.rows[0]["gold"] == "qualify"
    assert repairs.rows[0]["got"] == "none"
    # A correct repair scores independently of a wrong judgment elsewhere.
    wc02 = ge.run_case(next(c for c in ge.load_cases() if c.case_id == "wc-02"))
    assert ge.score_run(wc02)[0].outcome == "false_approval"
    assert ge.score_repairs([wc02]).rows  # the approved claim earns no repair
