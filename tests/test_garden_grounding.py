"""Claim-level grounding audit: ledger roundtrip, kinds, coverage honesty."""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.graph.garden import candidates as cand
from precis.graph.garden import grounding as g
from precis.graph.garden.proposals import pending_proposals
from precis.store import Store
from precis.store.types import ChunkInsert, Tag

S1 = "The ingest worker stores every body chunk before the embedder runs on them (pc1)."
S2 = "The embedder always finishes within one second for every stored document."
BODY = f"{S1} {S2}"


def _memory(store: Store, body: str = BODY, title: str = "Grounding fixture") -> int:
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


def _ok(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "i": i,
        "quote": (S1, S2)[i],
        "kind": "empirical",
        "status": "supported",
        "evidence": ["pc1"],
    }
    return {**base, **kw}


def test_spans_index_body_exactly_and_cap_sets_truncated() -> None:
    scan = g.extract_claim_spans(f"# T\n\n{BODY}\n\nShort one.\n", "T")
    assert [s[0] for s in scan.spans] == ["# T", S1, S2, "Short one."]
    assert scan.skipped_short == 0 and not scan.truncated
    body = f"{BODY}\n\n{BODY}"
    for text, (a, b) in g.extract_claim_spans(body, "T").spans:
        assert body[a:b] == text
    capped = g.extract_claim_spans(f"{S1} {S2} {S1}", "T", cap=2)
    assert len(capped.spans) == 2 and capped.truncated and capped.scan_bound == 2


def test_audit_roundtrip_and_not_in_pending_proposals(store: Store) -> None:
    rid = _memory(store)
    sha = store.target_sha("ref", rid)
    assert sha is not None
    rec = g.audit_body(BODY, "Grounding fixture", sha, call=_reply(_ok(0), _ok(1)))
    assert g.write_audit(store, rid, rec) is not None
    got = g.latest_audit(store, rid)
    assert got is not None and got.current and got.target_sha == sha
    assert [c.span for c in got.claims] == [c.span for c in rec.claims]
    assert got.coverage["all_grounded"] is False
    assert got.coverage["provisional"] == 2
    assert pending_proposals(store) == []


def test_definition_and_existence_claims_stay_distinct() -> None:
    definition = "A frobnicator is a paper-folding device"
    existence = "one exists in the lab"
    body = f"{definition}, and {existence}."
    call = _reply(
        _ok(0, quote=definition, kind="definition", evidence=[], subject="frobnicator"),
        _ok(0, quote=existence, kind="empirical", status="unsupported", evidence=[]),
    )
    rec = g.audit_body(body, "T", "sha", call=call)
    assert [(c.kind, c.status) for c in rec.claims] == [
        ("definition", "provisional"),
        ("empirical", "unresolved"),
    ]
    assert rec.coverage["all_grounded"] is False
    assert [body[a:b] for a, b in (c.span for c in rec.claims)] == [
        definition,
        existence,
    ]
    assert rec.coverage["claims_total"] is None
    assert rec.coverage["classifier_reported_claims"] == rec.coverage["inspected"] == 2
    assert rec.coverage["spans_total"] == rec.coverage["spans_inspected"] == 1


def test_one_reply_entry_cannot_establish_coverage_of_a_compound_sentence() -> None:
    definition = "A frobnicator is a paper-folding device"
    remainder = ", and one exists in the lab."
    body = definition + remainder
    for quote in (definition, body):
        rec = g.audit_body(
            body,
            "T",
            "sha",
            call=_reply(_ok(0, quote=quote, kind="definition", evidence=[])),
        )
        assert rec.coverage["classifier_reported_claims"] == 1
        assert rec.coverage["claims_total"] is rec.coverage["unchecked"] is None
        assert rec.coverage["claim_coverage_verified"] is False
        assert rec.coverage["all_grounded"] is False
        gaps = [body[a:b] for a, b in rec.coverage["unaccounted_ranges"]]
        assert gaps == ([remainder] if quote == definition else [])


@pytest.mark.parametrize("handle", ["chat:abc", "co7", "cc42", "conv:co7", "ag9"])
def test_chat_evidence_cannot_ground_empirical_claim(handle: str) -> None:
    body = S1.replace("pc1", handle)
    rec = g.audit_body(
        body,
        "T",
        "sha",
        call=_reply(_ok(0, quote=body, evidence=[handle])),
    )
    assert rec.claims[0].status == "unresolved"
    assert "chat evidence" in rec.claims[0].reason


def test_non_chat_handle_stays_provisional() -> None:
    body = S1.replace("pc1", "me1")
    rec = g.audit_body(
        body, "T", "sha", call=_reply(_ok(0, quote=body, evidence=["me1"]))
    )
    assert rec.claims[0].status == "provisional"


def test_unsupplied_handle_is_unknown() -> None:
    rec = g.audit_body(S1, "T", "sha", call=_reply(_ok(0, evidence=["pc999"])))
    assert rec.claims[0].status == "unresolved"
    assert rec.claims[0].evidence == []


def test_detector_failure_is_unchecked_not_unsupported() -> None:
    def boom(prompt: str) -> tuple[str, str | None]:
        raise RuntimeError("down")

    for call in (boom, lambda p: ("", "timeout"), lambda p: ("not json", None)):
        rec = g.audit_body(BODY, "T", "sha", call=call)
        assert {c.status for c in rec.claims} == {"unchecked"}
        assert rec.coverage["unchecked"] is None and rec.coverage["unresolved"] == 0
        assert rec.coverage["spans_unchecked"] == 2
        assert rec.coverage["all_grounded"] is False


def test_one_supported_one_unsupported_never_all_grounded() -> None:
    call = _reply(_ok(0), _ok(1, status="unsupported", evidence=[]))
    rec = g.audit_body(BODY, "T", "sha", call=call)
    assert rec.coverage["all_grounded"] is False
    assert rec.coverage["unresolved"] == 1 and rec.coverage["inspected"] == 2


def test_incomplete_scan_reports_truncated() -> None:
    scan = g.extract_claim_spans(BODY, "T", cap=1)
    rec_claims = g.classify_spans(scan, title="T", call=_reply(_ok(0)))
    cov = g.build_coverage(rec_claims, scan)
    assert cov["truncated"] is True and cov["all_grounded"] is False
    assert cov["claims_total"] is None and cov["spans_omitted_by_limit"] == 1


def test_omitted_spans_report_their_body_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Beyond the count cap the omitted sentence's exact offsets land in
    # unaccounted_ranges — the operator can tell which text went unchecked.
    scan = g.extract_claim_spans(BODY, "T", cap=1)
    assert [BODY[a:b] for a, b in scan.omitted_ranges] == [S2]
    cov = g.build_coverage(g.classify_spans(scan, title="T", call=_reply(_ok(0))), scan)
    assert [BODY[a:b] for a, b in cov["unaccounted_ranges"]] == [S2]

    # The character bound omits the same way.
    monkeypatch.setattr(g, "MAX_PROMPT_BODY_CHARS", len(S1) + 1)
    scan = g.extract_claim_spans(BODY, "T")
    assert scan.truncated and [BODY[a:b] for a, b in scan.omitted_ranges] == [S2]
    cov = g.build_coverage(g.classify_spans(scan, title="T", call=_reply(_ok(0))), scan)
    assert [BODY[a:b] for a, b in cov["unaccounted_ranges"]] == [S2]


def test_second_run_on_unchanged_sha_writes_no_new_record(store: Store) -> None:
    rid = _memory(store)
    calls: list[str] = []

    def call(prompt: str) -> tuple[str, str | None]:
        calls.append(prompt)
        return _reply(_ok(0), _ok(1))(prompt)

    nxt: dict[str, int] = {}
    candidates = g.grounding_audit_candidates(store, next_cursors=nxt)
    assert len(candidates) == 1
    assert g.audit_candidate(store, candidates[0], call=call) is not None
    first = [r for r in store.reviews_for("ref", rid) if r.actor == g.AUDIT_ACTOR]
    assert g.grounding_audit_candidates(store) == []
    second = [r for r in store.reviews_for("ref", rid) if r.actor == g.AUDIT_ACTOR]
    assert len(first) == len(second) == 1 and len(calls) == 1
    assert g.CURSOR_KEY in nxt


@pytest.mark.parametrize("response", [("", "timeout"), ("not JSON", None)])
def test_same_sha_retries_after_classifier_recovers(
    store: Store, response: tuple[str, str | None]
) -> None:
    rid = _memory(store)
    (candidate,) = g.grounding_audit_candidates(store)
    assert g.audit_candidate(store, candidate, call=lambda _: response) is not None
    failed = g.latest_audit(store, rid)
    assert failed is not None and failed.coverage["classifier_incomplete"]

    (retry,) = g.grounding_audit_candidates(store)
    assert g.audit_candidate(store, retry, call=_reply(_ok(0), _ok(1))) is not None
    recovered = g.latest_audit(store, rid)
    assert recovered is not None and not recovered.coverage["classifier_incomplete"]
    assert recovered.target_sha == failed.target_sha
    assert g.grounding_audit_candidates(store) == []


def test_pass_is_opt_in() -> None:
    assert "memory-grounding" in cand.OPT_IN_PASSES
    assert "grounding_audit" not in cand.detectors_for(("all",))
    assert cand.detectors_for(("memory-grounding",)) == ["grounding_audit"]


def test_short_assertion_missing_from_reply_leaves_coverage_unknown() -> None:
    rec = g.audit_body(
        f"{S1} The service never fails.", "T", "sha", call=_reply(_ok(0))
    )
    assert rec.claims[1].text == "The service never fails."
    assert rec.claims[1].status == "unchecked"
    assert rec.coverage["claims_total"] is None
    assert rec.coverage["spans_unchecked"] == 1
    assert rec.coverage["all_grounded"] is False


def test_stale_audit_cannot_bind_to_a_new_revision(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _memory(store)
    sha = store.target_sha("ref", rid)
    assert sha is not None
    rec = g.audit_body(BODY, "Grounding fixture", sha, call=_reply(_ok(0), _ok(1)))
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = 'Edited' WHERE ref_id = %s", (rid,))
    monkeypatch.setattr(store, "target_sha", lambda *args: sha)
    assert g.write_audit(store, rid, rec) is None
    assert g.latest_audit(store, rid) is None


def test_coordinator_shares_proposal_budget(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.graph.garden.runner import run_garden_tick

    first = _memory(store)
    second = _memory(store)
    monkeypatch.setattr(g, "_routed_call", lambda ref_id: _reply(_ok(0), _ok(1)))
    tick = run_garden_tick(store, passes=("memory-grounding",), max_proposals=1)
    assert tick.proposals_by_detector == {"grounding_audit": 1}
    assert g.CURSOR_KEY not in tick.cursors
    assert g.latest_audit(store, first) is not None
    assert g.latest_audit(store, second) is None
    tick = run_garden_tick(store, passes=("memory-grounding",), max_proposals=1)
    assert tick.proposals_by_detector == {"grounding_audit": 1}
    assert g.CURSOR_KEY in tick.cursors
    assert g.latest_audit(store, second) is not None


def test_router_pause_keeps_last_candidate_retryable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.graph.garden.runner import run_garden_tick
    from precis.utils.llm import router

    previous = _memory(store)
    first = _memory(store)
    last = _memory(store)
    calls: list[int | None] = []

    def route(request: router.LlmRequest) -> router.LlmResult:
        calls.append(request.ref_id)
        paused = request.ref_id == last and calls.count(last) == 1
        reply, _ = _reply(_ok(0), _ok(1))(request.prompt)
        return router.LlmResult(
            text="" if paused else reply,
            cost_usd=None,
            turns_used=None,
            model="fixture",
            tier=request.tier,
            error="quota paused" if paused else None,
            paused=paused,
        )

    monkeypatch.setattr(router, "route", route)
    saved = {g.CURSOR_KEY: previous}
    tick = run_garden_tick(store, passes=("memory-grounding",), cursors=saved)
    assert tick.paused and tick.llm_errors == 0
    assert tick.proposals_by_detector == {"grounding_audit": 1}
    assert g.CURSOR_KEY not in tick.cursors
    assert g.latest_audit(store, first) is not None
    assert g.latest_audit(store, last) is None

    retry = run_garden_tick(
        store, passes=("memory-grounding",), cursors={**saved, **tick.cursors}
    )
    assert not retry.paused
    assert retry.proposals_by_detector == {"grounding_audit": 1}
    assert g.CURSOR_KEY in retry.cursors
    assert g.latest_audit(store, last) is not None
    assert calls == [first, last, last]
