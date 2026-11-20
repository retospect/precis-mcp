"""Passage-grounded support verification for the grounding audit
(gr634486 slice 2): overreach, mislabeled studies, weak sources and
definition-kind claims, with source fidelity recorded separately from
evidential strength. The support seam is a stub; no LLM, no DB in the
unit cases — plus store-backed integration cases through
:func:`precis.graph.garden.grounding.audit_candidate`, including
cross-audit premise resolution (cycles, premise-supported inference,
self-citation).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from precis.graph.garden import grounding as g
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.taproot.reground import PaperChunk

PASSAGES = [PaperChunk(chunk_id=7, chunk_ord=0, section_path=None, text="passage")]


def _claim(
    kind: str = "empirical", text: str = "The catalyst doubles the yield"
) -> g.ClaimRecord:
    return g.ClaimRecord(span=[0, len(text)], text=text, kind=kind, evidence=["pc7"])


def _raw(**kw: Any) -> g.RawSupport:
    base: dict[str, Any] = {
        "supported": True,
        "source_fidelity": "faithful",
        "evidential_strength": "established",
        "study_type": "experiment",
        "quantities_match": True,
        "conditions_match": True,
    }
    base.update(kw)
    return g.RawSupport(**base)


def _stub(raw: g.RawSupport) -> g.SupportFn:
    return lambda _claim, _passages: raw


def test_single_hop_overreach_is_partial() -> None:
    verdict = g.verify_claim_support(
        _claim(), PASSAGES, support_fn=_stub(_raw(overreach=True))
    )
    assert verdict.verdict == "partial"
    assert "single-hop overreach" in verdict.flags


def test_simulation_presented_as_experiment_flagged() -> None:
    verdict = g.verify_claim_support(
        _claim(),
        PASSAGES,
        support_fn=_stub(
            _raw(study_type="simulation", evidential_strength="suggested")
        ),
    )
    assert "simulation presented as experiment" in verdict.flags
    assert verdict.verdict == "partial"
    assert verdict.study_type == "simulation"


def test_simulation_in_source_report_is_faithful_not_experiment() -> None:
    # A source_report faithfully reporting a simulation is not the
    # memory presenting it as an experiment — recorded, not downgraded.
    verdict = g.verify_claim_support(
        _claim(kind="source_report"),
        PASSAGES,
        support_fn=_stub(
            _raw(study_type="simulation", evidential_strength="suggested")
        ),
    )
    assert verdict.study_type == "simulation"
    assert verdict.verdict == "supported"
    assert "simulation presented as experiment" not in verdict.flags


def test_faithful_report_of_weak_paper_not_established() -> None:
    verdict = g.verify_claim_support(
        _claim(), PASSAGES, support_fn=_stub(_raw(evidential_strength="weak"))
    )
    assert verdict.source_fidelity == "faithful"
    assert verdict.evidential_strength == "weak"
    assert verdict.verdict == "partial"


def test_definition_never_supports_existence() -> None:
    verdict = g.verify_claim_support(
        _claim(kind="definition"), PASSAGES, support_fn=_stub(_raw())
    )
    assert verdict.verdict == "not_applicable"
    assert "meaning" in verdict.reason


def test_human_decision_and_hypothesis_have_own_checkers() -> None:
    decision = g.verify_claim_support(
        _claim(kind="human_decision"), PASSAGES, support_fn=_stub(_raw())
    )
    assert decision.verdict == "not_applicable"
    hypothesis = g.verify_claim_support(
        _claim(kind="hypothesis"), PASSAGES, support_fn=_stub(_raw())
    )
    assert hypothesis.verdict == "unresolved"


def test_missing_passage_or_failed_verify_is_unresolved_not_unsupported() -> None:
    no_passage = g.verify_claim_support(_claim(), [], support_fn=_stub(_raw()))
    assert no_passage.verdict == "unresolved"
    failed = g.verify_claim_support(
        _claim(),
        PASSAGES,
        support_fn=lambda _c, _p: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert failed.verdict == "unresolved"
    unavailable = g.verify_claim_support(
        _claim(), PASSAGES, support_fn=lambda _c, _p: None
    )
    assert unavailable.verdict == "unresolved"


def test_contradiction_beats_support() -> None:
    verdict = g.verify_claim_support(
        _claim(),
        PASSAGES,
        support_fn=_stub(_raw(contradicts=True, reason="measured the opposite")),
    )
    assert verdict.verdict == "contradicted"


def test_quantities_and_conditions_recorded_as_checked() -> None:
    verdict = g.verify_claim_support(
        _claim(), PASSAGES, support_fn=_stub(_raw(quantities_match=False))
    )
    assert verdict.quantities_checked
    assert verdict.conditions_checked
    assert verdict.verdict == "partial"


def test_source_copying_masquerading_as_corroboration_is_flagged() -> None:
    verdict = g.verify_claim_support(
        _claim(), PASSAGES, support_fn=_stub(_raw(source_copying=True))
    )
    assert verdict.verdict == "partial"
    assert "source copying masquerading as corroboration" in verdict.flags


def test_validated_passage_identity_is_retained() -> None:
    raw = _raw()
    raw.passage = {"chunk_id": 7, "chunk_ord": 0}
    verdict = g.verify_claim_support(_claim(), PASSAGES, support_fn=_stub(raw))
    assert verdict.passage == {"chunk_id": 7, "chunk_ord": 0}


# ── the live seam's verdict folding (pure; no LLM) ────────────────────────


def _judged(*pairs: tuple[dict[str, Any], dict[str, Any]]) -> list:
    return list(pairs)


def test_reviewer_verdict_empty_reply_is_unavailable() -> None:
    assert g.combine_reviewer_verdicts(_judged()) is None


def test_reviewer_verdict_contradiction_beats_support() -> None:
    raw = g.combine_reviewer_verdicts(
        _judged(
            ({"chunk_id": 1}, {"supports": "yes", "terminal": True}),
            ({"chunk_id": 2}, {"supports": "no", "contradicts": True}),
        )
    )
    assert raw is not None and raw.contradicts and not raw.supported


def test_reviewer_verdict_terminal_recitation_is_source_copying() -> None:
    raw = g.combine_reviewer_verdicts(
        _judged(
            ({"chunk_id": 1}, {"supports": "yes", "terminal": False}),
            ({"chunk_id": 2}, {"supports": "no"}),
        )
    )
    assert raw is not None
    assert raw.supported and raw.source_copying
    assert raw.evidential_strength == "suggested"
    assert raw.passage == {"chunk_id": 1}


def test_reviewer_verdict_same_setup_drives_condition_checks() -> None:
    raw = g.combine_reviewer_verdicts(
        _judged(({"chunk_id": 1}, {"supports": "yes", "same_setup": False}))
    )
    assert raw is not None
    assert raw.conditions_match is False
    assert raw.quantities_match is False


def test_reviewer_verdict_scoped_partial_is_overreach() -> None:
    raw = g.combine_reviewer_verdicts(
        _judged(
            ({"chunk_id": 1}, {"supports": "no"}),
            ({"chunk_id": 2}, {"supports": "partial", "same_setup": True}),
        )
    )
    assert raw is not None
    assert raw.supported and raw.overreach
    assert raw.conditions_match is True


def test_reviewer_verdict_all_no_is_a_real_negative() -> None:
    raw = g.combine_reviewer_verdicts(
        _judged(
            ({"chunk_id": 1}, {"supports": "no"}),
            ({"chunk_id": 2}, {"supports": "no"}),
        )
    )
    assert raw is not None and not raw.supported and not raw.contradicts


# ── store-backed integration: verification inside audit_candidate ────────


def _memory_with_paper_evidence(store: Store, text: str) -> tuple[int, int, str]:
    """A memory claiming ``text`` (which embeds the paper chunk handle)
    plus the paper + chunk it cites. Returns (memory_id, paper_id, handle)."""
    from precis.utils.handle_registry import try_format

    paper = store.insert_ref(
        kind="paper", slug="grounding-support-source", title="Source paper"
    )
    (chunk,) = store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="The catalyst doubles the yield in vitro.")]
    )
    handle = try_format("paper", int(chunk.id), chunk=True)
    assert handle is not None
    memory = store.insert_ref(
        kind="memory", slug=None, title="Grounding support fixture"
    )
    body = f"{text} ({handle})."
    store.chunks.insert_chunks(
        memory.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
    )
    store.add_tag(memory.id, Tag.open("section:reference"), set_by="system")
    return int(memory.id), int(paper.id), handle


def _classify_call(handle: str) -> g.Call:
    def call(prompt: str) -> tuple[str, str | None]:
        assert handle in prompt
        return (
            json.dumps(
                {
                    "claims": [
                        {
                            "i": 0,
                            "quote": "The catalyst doubles the yield",
                            "kind": "empirical",
                            "status": "supported",
                            "evidence": [handle],
                        }
                    ]
                }
            ),
            None,
        )

    return call


def test_audit_candidate_verifies_support_and_rolls_up(store: Store) -> None:
    rid, _paper_id, handle = _memory_with_paper_evidence(
        store, "The catalyst doubles the yield"
    )
    (candidate,) = g.grounding_audit_candidates(store)
    assert candidate.target_id == rid
    result = g.audit_candidate(
        store,
        candidate,
        call=_classify_call(handle),
        verify_fn=_stub(_raw()),
    )
    assert result is not None
    record = g.latest_audit(store, rid)
    assert record is not None
    (claim,) = [c for c in record.claims if c.kind == "empirical"]
    assert claim.support is not None and claim.support["verdict"] == "supported"
    assert claim.rollup == "supported"
    assert record.coverage["support"]["supported"] == 1
    assert record.coverage["rollup"]["supported"] == 1
    # No embedder wired: the conflict probe degrades honestly.
    assert claim.conflict is None
    assert record.coverage["conflict"]["not_probed"] == 1


def test_audit_candidate_verification_failure_stays_pending(store: Store) -> None:
    rid, _paper_id, handle = _memory_with_paper_evidence(
        store, "The catalyst doubles the yield"
    )
    (candidate,) = g.grounding_audit_candidates(store)
    result = g.audit_candidate(
        store,
        candidate,
        call=_classify_call(handle),
        verify_fn=_stub(_raw(supported=False, reason="passage does not contain it")),
    )
    assert result is not None
    record = g.latest_audit(store, rid)
    assert record is not None
    (claim,) = [c for c in record.claims if c.kind == "empirical"]
    assert claim.support is not None and claim.support["verdict"] == "unsupported"
    assert claim.rollup == "unsupported"


# ── store-backed integration: cross-audit premise resolution ─────────────


def _two_memories_citing_each_other(store: Store) -> tuple[int, int]:
    """Two memories whose bodies name each other's handles (handles exist
    only after both refs are inserted, so the body chunks come last).
    Returns (a_id, b_id)."""
    mem_a = store.insert_ref(kind="memory", slug=None, title="Cycle fixture A")
    mem_b = store.insert_ref(kind="memory", slug=None, title="Cycle fixture B")
    a, b = int(mem_a.id), int(mem_b.id)
    store.chunks.insert_chunks(
        mem_a.id,
        [
            ChunkInsert(
                ord=0,
                text=f"Claim alpha holds (me{b}).",
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    store.chunks.insert_chunks(
        mem_b.id,
        [
            ChunkInsert(
                ord=0,
                text=f"Claim beta holds (me{a}).",
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    store.add_tag(mem_a.id, Tag.open("section:reference"), set_by="system")
    store.add_tag(mem_b.id, Tag.open("section:reference"), set_by="system")
    return a, b


def _classify_premise_call(premise_handle: str, quote: str) -> g.Call:
    """A classifier reply whose claim premises on ``premise_handle`` (an
    inference kind, so no evidence is expected)."""

    def call(prompt: str) -> tuple[str, str | None]:
        assert premise_handle in prompt
        return (
            json.dumps(
                {
                    "claims": [
                        {
                            "i": 0,
                            "quote": quote,
                            "kind": "inference",
                            "status": "supported",
                            "premises": [premise_handle],
                        }
                    ]
                }
            ),
            None,
        )

    return call


def _candidate_for(store: Store, ref_id: int) -> g.GardenCandidate:
    by_id = {c.target_id: c for c in g.grounding_audit_candidates(store)}
    assert ref_id in by_id
    return by_id[ref_id]


def test_transitive_cross_audit_cycle_is_unsupported(store: Store) -> None:
    # meB premises on meA; meA premises back on meB. Auditing meB first
    # leaves it unresolved (meA carries no judgment yet); auditing meA
    # afterwards must catch the cycle through meB's recorded premises —
    # not only direct reciprocals.
    a, b = _two_memories_citing_each_other(store)
    result = g.audit_candidate(
        store,
        _candidate_for(store, b),
        call=_classify_premise_call(f"me{a}", "Claim beta holds"),
    )
    assert result is not None
    b_record = g.latest_audit(store, b)
    assert b_record is not None
    (b_claim,) = [c for c in b_record.claims if c.kind == "inference"]
    # No judgment to rest on yet: unresolved, not unsupported.
    assert b_claim.rollup == "unresolved"

    result = g.audit_candidate(
        store,
        _candidate_for(store, a),
        call=_classify_premise_call(f"me{b}", "Claim alpha holds"),
    )
    assert result is not None
    a_record = g.latest_audit(store, a)
    assert a_record is not None
    (a_claim,) = [c for c in a_record.claims if c.kind == "inference"]
    assert a_claim.rollup == "unsupported"
    assert a_record.coverage["lineage"]["cycles"] == [["c0"]]
    assert a_record.coverage["rollup"]["unsupported"] == 1


def test_premise_supported_inference_stays_supported(store: Store) -> None:
    # meA is passage-verified supported; meB infers from meA as its only
    # premise. Verified multi-step reasoning stays usable.
    rid_a, _paper_id, handle = _memory_with_paper_evidence(
        store, "The catalyst doubles the yield"
    )
    result = g.audit_candidate(
        store,
        _candidate_for(store, rid_a),
        call=_classify_call(handle),
        verify_fn=_stub(_raw()),
    )
    assert result is not None
    a_record = g.latest_audit(store, rid_a)
    assert a_record is not None
    (a_claim,) = [c for c in a_record.claims if c.kind == "empirical"]
    assert a_claim.rollup == "supported"

    mem_b = store.insert_ref(kind="memory", slug=None, title="Inference fixture")
    rid_b = int(mem_b.id)
    store.chunks.insert_chunks(
        mem_b.id,
        [
            ChunkInsert(
                ord=0,
                text=(f"The catalyst doubles the yield in vitro (me{rid_a})."),
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    store.add_tag(mem_b.id, Tag.open("section:reference"), set_by="system")
    classify = _classify_premise_call(
        f"me{rid_a}", "The catalyst doubles the yield in vitro"
    )

    def call(prompt: str) -> tuple[str, str | None]:
        if prompt.startswith("Review the inference warrant"):
            return json.dumps({"verdict": "supported", "reason": "Same scope"}), None
        return classify(prompt)

    result = g.audit_candidate(store, _candidate_for(store, rid_b), call=call)
    assert result is not None
    b_record = g.latest_audit(store, rid_b)
    assert b_record is not None
    (b_claim,) = [c for c in b_record.claims if c.kind == "inference"]
    assert b_claim.rollup == "supported"
    assert b_record.coverage["rollup"]["supported"] == 1


def test_self_premise_is_unsupported(store: Store) -> None:
    # A claim that premises on the very memory being audited grounds
    # nothing: circular on its face.
    memory = store.insert_ref(kind="memory", slug=None, title="Self-premise fixture")
    rid = int(memory.id)
    store.chunks.insert_chunks(
        memory.id,
        [
            ChunkInsert(
                ord=0,
                text=f"Claim alpha rests on itself (me{rid}).",
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    store.add_tag(memory.id, Tag.open("section:reference"), set_by="system")
    result = g.audit_candidate(
        store,
        _candidate_for(store, rid),
        call=_classify_premise_call(f"me{rid}", "Claim alpha rests on itself"),
    )
    assert result is not None
    record = g.latest_audit(store, rid)
    assert record is not None
    (claim,) = [c for c in record.claims if c.kind == "inference"]
    assert claim.rollup == "unsupported"
    assert record.coverage["lineage"]["cycles"] == [["c0"]]


def test_live_citation_without_premise_judgment_is_unresolved(store: Store) -> None:
    rid, _paper, handle = _memory_with_paper_evidence(
        store, "The catalyst doubles the yield"
    )
    result = g.audit_candidate(
        store,
        _candidate_for(store, rid),
        call=_classify_premise_call(handle, "The catalyst doubles the yield"),
    )
    assert result is not None
    record = g.latest_audit(store, rid)
    assert record is not None
    assert record.claims[0].rollup == "unresolved"
    assert record.coverage["lineage"]["unresolved_premises"] == {"c0": [handle]}


def test_audit_candidate_checks_local_premise_lineage(store: Store) -> None:
    rid, _paper, handle = _memory_with_paper_evidence(
        store,
        "The catalyst doubles the yield. "
        "The yield increases. The yield changes. The baseline differs. "
        "The sample freezes. The sample thaws",
    )
    quotes = [
        "The catalyst doubles the yield",
        "The yield increases",
        "The yield changes",
        "The baseline differs",
        "The sample freezes",
        "The sample thaws",
    ]
    dependencies = [
        [],
        [quotes[0]],
        [quotes[1], quotes[3]],
        [],
        [quotes[5]],
        [quotes[4]],
    ]

    def call(prompt: str) -> tuple[str, str | None]:
        if prompt.startswith("Review the inference warrant"):
            return json.dumps({"verdict": "supported", "reason": "Test warrant"}), None
        return json.dumps(
            {
                "claims": [
                    {
                        "i": i,
                        "quote": q,
                        "kind": "inference" if premises else "empirical",
                        "status": "supported",
                        "local_premises": premises,
                        "evidence": [handle] if i in (0, 3) else [],
                    }
                    for i, (q, premises) in enumerate(
                        zip(quotes, dependencies, strict=True)
                    )
                ]
            }
        ), None

    def verify(claim: g.ClaimRecord, _passages: Sequence[PaperChunk]) -> g.RawSupport:
        return _raw(supported=claim.text == quotes[0])

    assert (
        g.audit_candidate(
            store, _candidate_for(store, rid), call=call, verify_fn=verify
        )
        is not None
    )
    record = g.latest_audit(store, rid)
    assert record is not None
    assert [c.rollup for c in record.claims] == [
        "supported",
        "supported",
        "partial",
        "unsupported",
        "unsupported",
        "unsupported",
    ]
    assert record.claims[2].inference_depth == 2
    assert record.coverage["lineage"]["cycles"] == [["c4", "c5"]]


# ── gr634490: passage pins bind before the judgment and fail closed ──────


def test_pin_passages_returns_only_pinned(store: Store) -> None:
    """A chunk that yields no pin is not evidence the audit can bind:
    it is dropped from the returned passages and pins nothing, so the
    judgment never reads it."""
    paper = store.insert_ref(kind="paper", slug="pin-source", title="Source paper")
    (chunk,) = store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="p")])
    record = g.AuditRecord("sha", [], {}, {})
    claim = g.ClaimRecord([0, 1], "claim", evidence=[])
    live = PaperChunk(chunk_id=int(chunk.id), chunk_ord=0, section_path=None, text="p")
    gone = PaperChunk(chunk_id=424242, chunk_ord=0, section_path=None, text="x")
    pinned = g._pin_passages(store, record, claim, [live, gone])
    assert [p.chunk_id for p in pinned] == [int(chunk.id)]
    handle = f"pc{int(chunk.id)}"
    assert list(record.pins) == [handle]
    assert claim.passages == [handle]


def test_verify_record_support_fails_closed_when_pinning_fails(
    store: Store, monkeypatch: Any
) -> None:
    """A consulted passage that cannot be pinned degrades the claim to
    unresolved — never a verdict built on evidence with no bound
    revision."""
    rid, _paper_id, handle = _memory_with_paper_evidence(
        store, "The catalyst doubles the yield"
    )
    (candidate,) = g.grounding_audit_candidates(store)
    assert candidate.target_id == rid
    monkeypatch.setattr(g, "_pin_passages", lambda *_a, **_k: [])
    result = g.audit_candidate(
        store,
        candidate,
        call=_classify_call(handle),
        verify_fn=_stub(_raw()),
    )
    assert result is not None
    record = g.latest_audit(store, rid)
    assert record is not None
    (claim,) = [c for c in record.claims if c.kind == "empirical"]
    assert claim.support is not None
    assert claim.support["verdict"] == "unresolved"
    assert claim.support["reason"] == "no candidate passage for the cited evidence"
    assert claim.passages == []


def test_conflict_probe_pins_before_judging_and_fails_closed(
    store: Store, monkeypatch: Any
) -> None:
    """The live probe binds the candidate's revision before the verifier
    reads it, and a candidate that cannot be pinned counts as verify
    unavailable — never a clean no-conflict pass (gr634490)."""
    from types import SimpleNamespace

    from precis.utils.handle_registry import try_format

    paper = store.insert_ref(kind="paper", slug="probe-source", title="Source paper")
    (chunk,) = store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="oppose")])
    handle = try_format("paper", int(chunk.id), chunk=True)
    assert handle is not None
    memory = store.insert_ref(kind="memory", slug=None, title="Probe target")
    store.chunks.insert_chunks(
        memory.id,
        [
            ChunkInsert(
                ord=0,
                text=f"The catalyst doubles the yield ({handle}).",
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    store.add_tag(memory.id, Tag.open("section:reference"), set_by="system")
    record = g.AuditRecord("sha", [], {}, {})
    claim = g.ClaimRecord(
        [0, 30],
        "The catalyst doubles the yield",
        kind="empirical",
        status="provisional",
    )
    block = SimpleNamespace(id=int(chunk.id), ord=0, text="oppose")
    ref_ns = SimpleNamespace(id=int(paper.id), kind="paper")

    monkeypatch.setattr(
        "precis.utils.embed_query.embed_query", lambda _e, _q: [0.1] * 8
    )
    monkeypatch.setattr(
        "precis.workers.conflict_search.negate_claim", lambda *_a: {"paraphrases": []}
    )
    monkeypatch.setattr(
        store.chunks,
        "search_chunks",
        lambda **_k: [(block, ref_ns, 0.1)],
    )
    monkeypatch.setattr(
        "precis.workers.conflict_search._attach_read_first", lambda _s, _c: None
    )
    monkeypatch.setattr(
        "precis.workers.conflict_search._select_for_verify",
        lambda cands, _n: list(cands),
    )

    pinned_at_verify: list[bool] = []

    def fake_verify(**_kw: Any) -> dict[str, Any]:
        # The pin must already hold when the judgment runs.
        pinned_at_verify.append(handle in record.pins)
        return {"contradicts": False}

    monkeypatch.setattr(
        "precis.workers._chase_llm._verify_support_with_caveats", fake_verify
    )
    probe = g._run_conflict_probe(store, int(memory.id), record, claim, object())
    assert probe.outcome == "none_found"
    assert pinned_at_verify == [True]
    assert claim.passages == [handle]

    # The chunk dies before the probe: the candidate cannot be pinned, so
    # the probe knows nothing — never a none_found on an unpinned passage.
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (int(chunk.id),))
    record2 = g.AuditRecord("sha2", [], {}, {})
    claim2 = g.ClaimRecord([0, 30], "The catalyst doubles the yield", kind="empirical")
    probe2 = g._run_conflict_probe(store, int(memory.id), record2, claim2, object())
    assert probe2.outcome == "unknown"
    assert "verify unavailable" in probe2.unknowns
    assert pinned_at_verify == [True]  # the dead chunk was never judged
