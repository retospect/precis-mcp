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
    # The unchanged memory stays eligible as the baseline starvation-guard
    # slot — listing it writes nothing; only an audit call does.
    again = g.grounding_audit_candidates(store)
    assert [c.target_id for c in again] == [rid]
    assert again[0].payload["baseline"] is True
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
    # Recovered and current: the memory drops out of the need queue and
    # remains only as the baseline starvation-guard slot.
    remaining = g.grounding_audit_candidates(store)
    assert [c.target_id for c in remaining] == [rid]
    assert remaining[0].payload["baseline"] is True


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


def test_priority_order_and_starvation_slot(store: Store) -> None:
    """Never-audited first, then stale (most human-authored first), and the
    4th slot re-audits the least-recently-audited current memory."""
    m1 = _memory(store)
    m2 = _memory(store)
    m3 = _memory(store)
    m4 = _memory(store)
    m5 = _memory(store)

    def audit(rid: int) -> None:
        (candidate,) = [
            c for c in g.grounding_audit_candidates(store) if c.target_id == rid
        ]
        assert g.audit_candidate(store, candidate, call=_reply(_ok(0), _ok(1)))

    # m2 and m4 are audited and still current; m3 is audited then edited
    # (stale), with the most human reads in the stale class.
    audit(m2)
    audit(m4)
    audit(m3)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = 'Edited' WHERE ref_id = %s", (m3,))
        conn.execute(
            "INSERT INTO ref_reads (ref_id, n_agent, n_human) VALUES (%s, 0, 5) "
            "ON CONFLICT (ref_id) DO UPDATE SET n_human = 5",
            (m3,),
        )

    candidates = g.grounding_audit_candidates(store)
    order = [c.target_id for c in candidates]
    # never-audited (scan order) → stale → the 4th slot is the
    # oldest-audited baseline (m2, audited before m4), then the rest.
    assert order == [m1, m5, m3, m2, m4]
    assert candidates[3].payload["baseline"] is True
    assert candidates[0].payload.get("baseline") is False
    assert candidates[2].payload.get("baseline") is False

    # The baseline slot re-audits a current memory: the dedup skip does not
    # apply to it, and a fresh row lands.
    n_before = len(
        [r for r in store.reviews_for("ref", m2) if r.actor == g.AUDIT_ACTOR]
    )
    assert g.audit_candidate(store, candidates[3], call=_reply(_ok(0), _ok(1)))
    n_after = len([r for r in store.reviews_for("ref", m2) if r.actor == g.AUDIT_ACTOR])
    assert n_after == n_before + 1

    # With no need candidate left the baseline arm survives even a
    # sub-threshold window — a lone current memory is never verified once
    # and then starved. Need candidates still take precedence in a small
    # window: they hold the early slots and the baseline arm yields — but
    # the set-aside slot is returned as an owed debt, never dropped.
    row = g._WindowRow(ref_id=m4, body=BODY, sha="sha", audited_at=None)
    kept, owed = g._interleave_baseline([], [row])
    assert [r.ref_id for r in kept] == [m4] and owed == []
    need_row = g._WindowRow(ref_id=m1, body=BODY, sha="sha", audited_at=None)
    kept, owed = g._interleave_baseline([need_row], [row])
    assert [r.ref_id for r in kept] == [m1]
    assert [r.ref_id for r in owed] == [m4]


def test_baseline_slot_survives_budget_limited_ticks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The starvation guard cannot itself starve: with a one-audit tick
    budget and need candidates that never go away (a classifier that
    never recovers keeps them stale), an unprocessed baseline candidate
    is owed and promoted ahead of the queue on the next tick."""
    from precis.graph.garden.runner import run_garden_tick

    healthy = _memory(store, title="Healthy")
    stale_ids = [_memory(store, title=f"Stale {i}") for i in range(3)]

    def audit(rid: int, call: g.Call | None = None) -> None:
        (candidate,) = [
            c for c in g.grounding_audit_candidates(store) if c.target_id == rid
        ]
        assert g.audit_candidate(store, candidate, call=call or _reply(_ok(0), _ok(1)))

    audit(healthy)
    # Three memories audit to classifier-incomplete rows: they stay stale
    # need candidates on every tick, ahead of the baseline slot.
    for rid in stale_ids:
        audit(rid, call=lambda p: ("not json", None))
    monkeypatch.setattr(g, "_routed_call", lambda _rid: lambda p: ("not json", None))
    cursors: dict[str, int] = {}
    for _ in range(4):
        tick = run_garden_tick(
            store, passes=("memory-grounding",), max_proposals=1, cursors=cursors
        )
        cursors = dict(tick.cursors)
        n = len(
            [r for r in store.reviews_for("ref", healthy) if r.actor == g.AUDIT_ACTOR]
        )
        if n >= 2:
            break
    # Without the owed-slot marker the three stale memories spend the
    # whole budget every tick and the healthy memory is never re-audited.
    assert n >= 2


def test_small_window_baseline_debt_is_recorded_and_paid(store: Store) -> None:
    """A baseline slot the small-window rule sets aside is recorded as an
    owed debt (the runner can only persist what it sees), promoted ahead
    of the need queue on the next collection, and cleared when paid — the
    job merges saved cursors under the run's, so an absent key would
    resurrect the debt and promote the same memory every tick (gr634490)."""
    stale = _memory(store, title="Stale need")
    baseline = _memory(store, title="Baseline")

    def audit(rid: int) -> None:
        (candidate,) = [
            c for c in g.grounding_audit_candidates(store) if c.target_id == rid
        ]
        assert g.audit_candidate(store, candidate, call=_reply(_ok(0), _ok(1)))

    audit(stale)
    audit(baseline)
    # The stale memory stays eligible forever: the edit left the audit row
    # unbound, so every window carries a need candidate.
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = 'Edited' WHERE ref_id = %s", (stale,))

    # First window: two rows, one persistent need candidate — the
    # small-window rule yields to need and the dropped baseline slot
    # lands in the next cursors as a debt.
    nxt: dict[str, int] = {}
    candidates = g.grounding_audit_candidates(store, cursors={}, next_cursors=nxt)
    assert [c.target_id for c in candidates] == [stale]
    assert candidates[0].payload.get("baseline") is False
    assert nxt[g.BASELINE_DUE_KEY] == baseline

    # Second collection: the owed memory overrides the yield, promotes to
    # the head of the queue, and paying the debt clears the marker
    # instead of leaving it to be merged back in.
    nxt2: dict[str, int] = {}
    candidates = g.grounding_audit_candidates(
        store, cursors=dict(nxt), next_cursors=nxt2
    )
    assert candidates[0].target_id == baseline
    assert candidates[0].payload["baseline"] is True
    assert nxt2[g.BASELINE_DUE_KEY] == 0


def test_moved_judgment_queues_dependency_consumers(store: Store) -> None:
    """A stale judgment queues the drafts and quests that actually
    depend on the memory — through the existing memory-review todo
    path — while an arbitrary inbound link is not a consumer."""
    from precis.utils.memory_hubs import review_todos

    mid = _memory(store)
    draft = store.insert_ref(
        kind="draft", slug="depending-draft", title="Depending draft"
    )
    store.add_link(
        src_ref_id=draft.id, dst_ref_id=mid, relation="cites", set_by="system"
    )
    bystander = store.insert_ref(
        kind="draft", slug="bystander-draft", title="Bystander"
    )
    store.add_link(
        src_ref_id=bystander.id,
        dst_ref_id=mid,
        relation="part-of",
        set_by="system",
    )
    assert g.queue_affected_consumers(store, mid) == 1
    assert review_todos(store, [int(draft.id)])
    assert not review_todos(store, [int(bystander.id)])
    # Dedup: an open review todo already covers the dependency.
    assert g.queue_affected_consumers(store, mid) == 0


@pytest.mark.parametrize("unchanged_partial", [False, True])
def test_reaudit_invalidating_one_claim_queues_consumers(
    store: Store, unchanged_partial: bool
) -> None:
    """The consumer queue fires on per-claim coverage change, not the
    memory's worst verdict: a source edit plus a verification outage
    invalidates one supported claim while another claim keeps the worst
    verdict unchanged — actual dependent consumers must still be queued
    (gr634490)."""
    from precis.utils.memory_hubs import review_todos

    paper = store.insert_ref(kind="paper", slug="source-paper", title="Source paper")
    store.chunks.insert_chunks(
        paper.id,
        [ChunkInsert(ord=0, text="The ingest worker stores every body chunk.")],
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (int(paper.id),)
        ).fetchone()
    assert row is not None
    handle = f"pc{int(row[0])}"
    body = f"{S1[: -len(' (pc1).')]} ({handle}). {S2}"
    mid = _memory(store, body=body)
    draft = store.insert_ref(
        kind="draft", slug="depending-draft", title="Depending draft"
    )
    store.add_link(
        src_ref_id=draft.id, dst_ref_id=mid, relation="cites", set_by="system"
    )

    def _supported(claim: g.ClaimRecord, passages: Any) -> g.RawSupport:
        p = passages[0]
        return g.RawSupport(
            supported=True,
            evidential_strength="weak" if unchanged_partial else "established",
            passage={"chunk_id": int(p.chunk_id), "chunk_ord": 0},
        )

    def _call() -> g.Call:
        return _reply(
            _ok(0, quote=f"{S1[: -len(' (pc1).')]} ({handle}).", evidence=[handle]),
            _ok(1, kind="definition", evidence=[]),
        )

    (candidate,) = [
        c for c in g.grounding_audit_candidates(store) if c.target_id == mid
    ]
    assert g.audit_candidate(store, candidate, call=_call(), verify_fn=_supported)
    prior = g.latest_audit(store, mid)
    assert prior is not None
    prior_worst = g._audit_worst_verdict(prior)[0]
    # The second claim has no evidence: the worst verdict stays
    # unresolved throughout — the old worst-verdict trigger never fired.
    assert prior_worst == "unresolved"
    before = g.grounding_summary(store, mid)
    assert before.supported == (0 if unchanged_partial else 1)
    assert before.partial == (1 if unchanged_partial else 0)
    assert before.unresolved == 1

    # The cited source changes and the re-audit's verifier is out: the
    # citing claim loses its support while the worst verdict is unchanged.
    store.chunks.insert_chunks(
        paper.id,
        [ChunkInsert(ord=1, text="The embedder stores each body chunk after.")],
    )
    (candidate,) = [
        c for c in g.grounding_audit_candidates(store) if c.target_id == mid
    ]
    assert candidate.action == "audit" and not candidate.payload.get("baseline")
    assert g.audit_candidate(
        store,
        candidate,
        call=_call(),
        verify_fn=_supported if unchanged_partial else lambda _c, _p: None,
    )
    current = g.latest_audit(store, mid)
    assert current is not None
    assert g._audit_worst_verdict(current)[0] == prior_worst
    after = g.grounding_summary(store, mid)
    assert after.supported == 0
    if unchanged_partial:
        assert g._consumer_view(current) == g._consumer_view(prior)
        assert after.partial == 1
    # And the dependent draft is queued through the review-todo path.
    assert review_todos(store, [int(draft.id)])


def test_priority_ranks_never_audited_by_draft_quest_use(store: Store) -> None:
    """Use is demand for current support, not just human reads: a
    never-audited memory that drafts and quests depend on ranks ahead of
    an equally unread one, in both the never-audited and stale buckets."""
    plain = _memory(store, title="Plain")
    depended = _memory(store, title="Depended on")
    stale_plain = _memory(store, title="Stale plain")
    stale_depended = _memory(store, title="Stale depended")

    def audit(rid: int) -> None:
        (candidate,) = [
            c for c in g.grounding_audit_candidates(store) if c.target_id == rid
        ]
        assert g.audit_candidate(store, candidate, call=_reply(_ok(0), _ok(1)))

    audit(stale_plain)
    audit(stale_depended)
    draft = store.insert_ref(
        kind="draft", slug="draft-consumer", title="Draft consumer"
    )
    store.add_link(
        src_ref_id=draft.id, dst_ref_id=depended, relation="cites", set_by="system"
    )
    quest = store.insert_ref(kind="quest", slug=None, title="Quest consumer")
    store.add_link(
        src_ref_id=quest.id,
        dst_ref_id=stale_depended,
        relation="cites",
        set_by="system",
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = 'Edited' WHERE ref_id = ANY(%s)",
            ([stale_plain, stale_depended],),
        )

    order = [c.target_id for c in g.grounding_audit_candidates(store)]
    # never-audited: the depended-on memory first …
    assert order.index(depended) < order.index(plain)
    # … then stale: the stale bucket ranks by use too, so the quest's
    # dependency outranks the equally stale plain memory.
    assert order.index(stale_depended) < order.index(stale_plain)
    assert order.index(plain) < order.index(stale_depended)


def test_consumer_fanout_continues_past_first_page(store: Store) -> None:
    from precis.utils.memory_hubs import review_todos

    mid = _memory(store)
    consumers = []
    for i in range(52):
        draft = store.insert_ref(kind="draft", slug=None, title=f"Consumer {i}")
        consumers.append(int(draft.id))
        store.add_link(
            src_ref_id=draft.id, dst_ref_id=mid, relation="cites", set_by="system"
        )
    assert g.queue_affected_consumers(store, mid) == 52
    assert len(review_todos(store, consumers)) == 52
    assert g.queue_affected_consumers(store, mid) == 0
