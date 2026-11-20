"""Claim-level grounding summary (gr634490 slice 1).

Coverage counts partition the substantive-claim denominator; freshness is
derived from the pinned target sha, the audit version and the cited-source
pins — a fresh timestamp alone renews nothing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.errors import NotFound
from precis.graph.garden import grounding as g
from precis.store import Store
from precis.store.types import ChunkInsert, Tag

S1 = "The ingest worker stores every body chunk before the embedder runs."
S2 = "A chunk is the smallest addressable text unit."


def _memory(store: Store, body: str, title: str = "Grounding summary fixture") -> int:
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


def _claim(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "i": i,
        "quote": (S1, S2)[i],
        "kind": "empirical",
        "status": "supported",
        "evidence": [],
    }
    return {**base, **kw}


def _audit(
    store: Store,
    rid: int,
    body: str,
    *entries: dict[str, Any],
    rollups: tuple[tuple[int, str], ...] = (),
) -> g.AuditRecord:
    sha = store.target_sha("ref", rid)
    assert sha is not None
    rec = g.audit_body(body, "Grounding summary fixture", sha, call=_reply(*entries))
    for idx, verdict in rollups:
        rec.claims[idx].rollup = verdict
    assert g.write_audit(store, rid, rec) is not None
    return rec


def test_edit_after_audit_is_stale(store: Store) -> None:
    rid = _memory(store, f"{S1} {S2}")
    _audit(
        store,
        rid,
        f"{S1} {S2}",
        _claim(0, kind="definition"),
        _claim(1, kind="definition"),
    )
    fresh = g.grounding_summary(store, rid)
    assert fresh.freshness == "current"
    # Classifier-only suggestions (definitions, unverified empiricals)
    # are unverified: suggested support lands in unresolved, never in
    # supported.
    assert fresh.supported == 0 and fresh.unresolved == fresh.claims_total == 2
    # An edit moves the target sha: the audit no longer binds to this
    # content, so nothing reads as supported even though the audit row is
    # the newest review on the memory.
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = 'Edited' WHERE ref_id = %s", (rid,))
    stale = g.grounding_summary(store, rid)
    assert stale.freshness == "stale"
    assert stale.stale_pins == (f"me{rid}",)
    assert stale.supported == 0
    assert stale.unresolved == stale.claims_total == 2
    line = g.summary_line(stale)
    assert "0 supported" not in line  # zero buckets are omitted …
    assert "2 unresolved" in line and "- stale" in line


def test_changed_source_stales_only_claims_citing_it(store: Store) -> None:
    source = store.insert_ref(kind="memory", slug=None, title="Cited source")
    store.chunks.insert_chunks(
        source.id,
        [ChunkInsert(ord=0, text="source body", meta={"chunk_kind": "memory_body"})],
    )
    src_handle = f"me{source.id}"
    body = f"{S1[:-1]} ({src_handle}). {S2}"
    rid = _memory(store, body)
    s1 = f"{S1[:-1]} ({src_handle})."
    _audit(
        store,
        rid,
        body,
        _claim(0, quote=s1, evidence=[src_handle]),
        _claim(1),
        rollups=((1, "supported"),),
    )
    before = g.grounding_summary(store, rid)
    assert before.freshness == "current"
    # Claim 0 is a classifier-only suggestion (unverified → unresolved);
    # claim 1 carried a rollup verdict at audit time.
    assert before.supported == 1 and before.unresolved == 1
    assert before.stale_pins == ()
    # The cited source changes: only the claim citing it loses support.
    store.chunks.insert_chunks(
        source.id,
        [ChunkInsert(ord=1, text="added later", meta={"chunk_kind": "memory_body"})],
    )
    after = g.grounding_summary(store, rid)
    assert after.freshness == "stale"
    assert after.stale_pins == (src_handle,)
    assert after.supported == 1
    assert after.unresolved == 1
    by_text = {c.text: c for c in after.claims}
    citing = by_text[s1]
    assert citing.status == "unresolved" and citing.recorded == "provisional"
    assert citing.stale_pins == (src_handle,)
    untouched = by_text[S2]
    assert untouched.status == "supported" and untouched.stale_pins == ()


def test_moved_passage_pin_stales_the_claim_that_read_it(store: Store) -> None:
    """A claim citing a finding pins the passage the verifier actually
    read, not only the finding: the underlying paper changes → exactly
    the citing claim loses support, even though the cited finding's own
    sha never moved (gr634490)."""
    paper = store.insert_ref(kind="paper", slug="source-paper", title="Source paper")
    store.chunks.insert_chunks(
        paper.id,
        [
            ChunkInsert(
                ord=0, text="The ingest worker stores every body chunk in the store."
            )
        ],
    )
    finding = store.insert_ref(kind="finding", slug=None, title="Claim hub")
    store.add_link(
        src_ref_id=finding.id,
        dst_ref_id=paper.id,
        relation="derived-from",
        set_by="system",
    )
    fh = f"fi{finding.id}"
    s1 = f"{S1[:-1]} ({fh})."
    body = f"{s1} {S2}"
    rid = _memory(store, body)
    (cand,) = g.grounding_audit_candidates(store)

    def support(claim: g.ClaimRecord, passages: Any) -> g.RawSupport:
        p = passages[0]
        return g.RawSupport(
            supported=True,
            evidential_strength="established",
            passage={"chunk_id": int(p.chunk_id), "chunk_ord": 0},
        )

    got = g.audit_candidate(
        store,
        cand,
        call=_reply(
            _claim(0, quote=s1, evidence=[fh]),
            _claim(1, kind="definition"),
        ),
        verify_fn=support,
    )
    assert got is not None
    rec = g.latest_audit(store, rid)
    assert rec is not None and rec.claims[0].passages
    before = g.grounding_summary(store, rid)
    # The passage-verified claim carries its rollup; the definition is a
    # classifier-only suggestion — unverified, so unresolved.
    assert before.freshness == "current" and before.supported == 1
    assert before.unresolved == 1
    # The paper's chunks move; the finding itself never did.
    store.chunks.insert_chunks(
        paper.id,
        [
            ChunkInsert(
                ord=1, text="The embedder stores each body chunk after a revision."
            )
        ],
    )
    after = g.grounding_summary(store, rid)
    assert after.freshness == "stale"
    citing = next(c for c in after.claims if c.text == s1)
    assert citing.status == "unresolved"
    assert citing.stale_pins and all(h.startswith("pc") for h in citing.stale_pins)
    assert citing.passage in citing.stale_pins
    assert next(c for c in after.claims if c.text == S2).status == "unresolved"


def test_replaced_premise_judgment_stales_the_dependent_claim(store: Store) -> None:
    """A premise binding is to the judgment, not only the sha: a
    replacement audit of the premise memory carrying a different verdict
    (same body!) stales the dependent inference — the certificate the
    audit bound is gone even though the premise's sha never moved."""
    paper = store.insert_ref(kind="paper", slug="source-paper", title="Source paper")
    store.chunks.insert_chunks(
        paper.id,
        [
            ChunkInsert(
                ord=0, text="The ingest worker stores every body chunk in the store."
            )
        ],
    )
    pp = f"pa{paper.id}"
    b_body = f"{S1[:-1]} ({pp})."
    bid = _memory(store, b_body, title="Premise memory")
    inference = f"The pipeline is therefore latency-bound (me{bid})."
    a_body = f"{S2} {inference}"
    aid = _memory(store, a_body, title="Dependent memory")

    def _support(ok: bool) -> Any:
        def fn(claim: g.ClaimRecord, passages: Any) -> g.RawSupport:
            p = passages[0]
            return g.RawSupport(
                supported=ok,
                evidential_strength="established",
                passage={"chunk_id": int(p.chunk_id), "chunk_ord": 0},
            )

        return fn

    def _call(*entries: dict[str, Any]) -> g.Call:
        def call(prompt: str) -> tuple[str, str | None]:
            if "inference warrant" in prompt:
                return json.dumps(
                    {"verdict": "supported", "reason": "premises warrant it"}
                ), None
            return _reply(*entries)(prompt)

        return call

    def _audit(rid: int, entries: list[dict[str, Any]], verify: Any) -> None:
        (cand,) = [c for c in g.grounding_audit_candidates(store) if c.target_id == rid]
        assert (
            g.audit_candidate(store, cand, call=_call(*entries), verify_fn=verify)
            is not None
        )

    _audit(bid, [_claim(0, quote=b_body, evidence=[pp])], _support(True))
    _audit(
        aid,
        [
            _claim(0, quote=S2, kind="definition"),
            _claim(
                1,
                quote=inference,
                kind="inference",
                premises=[f"me{bid}"],
                evidence=[],
            ),
        ],
        _support(True),
    )
    rec = g.latest_audit(store, aid)
    assert rec is not None
    assert rec.premise_pins[f"me{bid}"]["verdict"] == "supported"
    before = g.grounding_summary(store, aid)
    # The inference is warrant- and premise-verified; the bare definition
    # is a classifier-only suggestion and stays unresolved.
    assert before.freshness == "current" and before.supported == 1
    assert before.unresolved == 1
    # The premise's judgment is replaced — same body, a new audit row
    # with an unsupported verdict (a moved source, a re-check): the
    # certificate the dependent bound is gone.
    _audit(bid, [_claim(0, quote=b_body, evidence=[pp])], _support(False))
    after = g.grounding_summary(store, aid)
    assert after.freshness == "stale"
    dependent = next(c for c in after.claims if c.text == inference)
    assert dependent.status == "unresolved"
    assert f"me{bid}" in dependent.stale_pins
    # The local definition never carried a certificate: it stays
    # unresolved, not demoted.
    assert next(c for c in after.claims if c.text == S2).status == "unresolved"
    # And the audit history labels the moved pin, not just "stale".
    assert any(f"me{bid}" in e.stale_pins for e in after.history[:1])


def test_local_premise_fallout_propagates_to_a_fixpoint() -> None:
    """A conclusion cannot stay supported on a premise that lands
    unresolved later in the body: the read propagates premise fallout to
    a fixpoint rather than one body-order pass (gr634490)."""
    rec = g.AuditRecord(
        "sha",
        [
            g.ClaimRecord(
                [0, 10],
                "conclusion",
                kind="inference",
                status="supported",
                rollup="supported",
                local_premises=["middle"],
            ),
            g.ClaimRecord(
                [10, 20],
                "middle",
                kind="inference",
                status="supported",
                rollup="supported",
                local_premises=["root"],
            ),
            g.ClaimRecord(
                [20, 30],
                "root",
                kind="empirical",
                status="supported",
                rollup="supported",
            ),
        ],
        {},
        {},
    )
    eff = g._effective_statuses(rec, {0: "supported", 1: "supported", 2: "unresolved"})
    assert eff == {0: "unresolved", 1: "unresolved", 2: "unresolved"}
    # The propagation never upgrades: an unresolved claim stays put.
    assert g._effective_statuses(
        rec, {0: "unresolved", 1: "unresolved", 2: "supported"}
    ) == {
        0: "unresolved",
        1: "unresolved",
        2: "supported",
    }


def test_never_audited_reports_none(store: Store) -> None:
    rid = _memory(store, f"{S1} {S2}")
    summary = g.grounding_summary(store, rid)
    assert summary.freshness == "none"
    assert summary.audited_at is None
    assert summary.claims_total == 0
    assert g.summary_line(summary) == "grounding: not audited"
    assert g.grounding_line(store, rid) == "grounding: not audited"


def test_truncated_scan_is_reported(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _memory(store, f"{S1} {S2}")
    sha = store.target_sha("ref", rid)
    assert sha is not None
    scan = g.extract_claim_spans(f"{S1} {S2}", "T", cap=1)
    claims = g.classify_spans(scan, title="T", call=_reply(_claim(0)))
    rec = g.AuditRecord(sha, claims, g.build_coverage(claims, scan), g.versions())
    assert g.write_audit(store, rid, rec) is not None
    summary = g.grounding_summary(store, rid)
    assert summary.truncated is True
    assert "truncated scan" in g.summary_line(summary)
    # An incomplete audit renders the reported count as a lower bound: the
    # classifier's entries are not the true claim total.
    assert "1+ claims" in g.summary_line(summary)
    # A classifier failure (all spans unchecked) reports incompleteness too.
    rid2 = _memory(store, f"{S1} {S2}", title="Other fixture")
    sha2 = store.target_sha("ref", rid2)
    assert sha2 is not None
    rec2 = g.audit_body(
        f"{S1} {S2}", "Other fixture", sha2, call=lambda p: ("", "timeout")
    )
    assert g.write_audit(store, rid2, rec2) is not None
    summary2 = g.grounding_summary(store, rid2)
    assert summary2.truncated is True
    assert summary2.unchecked == summary2.claims_total == 2
    assert summary2.supported == 0


def test_summary_missing_memory_refuses(store: Store) -> None:
    with pytest.raises(NotFound):
        g.grounding_summary(store, 999999)


def test_old_prompt_version_row_reads_stale(store: Store) -> None:
    """A row recorded under an older prompt/policy version is stale on
    recall exactly when scheduling re-queues it: version and premise
    checks agree across recall and scheduling (gr634490)."""
    rid = _memory(store, f"{S1} {S2}")
    sha = store.target_sha("ref", rid)
    assert sha is not None
    rec = g.audit_body(
        f"{S1} {S2}",
        "Grounding summary fixture",
        sha,
        call=_reply(_claim(0, kind="definition"), _claim(1, kind="definition")),
    )
    rec.versions = {"audit": g.AUDIT_VERSION, "prompt": "grounding-classify.v0"}
    assert g.write_audit(store, rid, rec) is not None
    # The content sha never moved — only the recorded policy version is
    # old. Recall must not certify judgments a current policy would not.
    summary = g.grounding_summary(store, rid)
    assert summary.freshness == "stale"
    assert summary.supported == 0
    assert summary.unresolved == summary.claims_total == 2
    # And scheduling agrees: the memory re-enters the queue as stale need.
    (candidate,) = g.grounding_audit_candidates(store)
    assert candidate.target_id == rid
    assert candidate.action == "audit"
    assert candidate.payload.get("baseline") is False
    # The kept history row names the state; the recorded judgment stays
    # inspectable in the drill-down.
    assert summary.history[0].current is False


def test_unpinned_citation_never_renders_supported(store: Store) -> None:
    """A cited handle that did not resolve at audit time certifies nothing:
    the classifier suggestion lands in unresolved, and the row itself stays
    current — a re-audit cannot pin a handle that does not exist."""
    body = f"{S1[:-1]} (pc424242). {S2}"
    rid = _memory(store, body)
    s1 = f"{S1[:-1]} (pc424242)."
    _audit(
        store,
        rid,
        body,
        _claim(0, quote=s1, evidence=["pc424242"]),
        _claim(1, kind="definition"),
    )
    summary = g.grounding_summary(store, rid)
    assert summary.freshness == "current"
    assert summary.stale_pins == ()
    # The unpinned citation certifies nothing — and the bare definition
    # beside it is a classifier-only suggestion, so nothing here is
    # supported.
    assert summary.supported == 0
    assert summary.unresolved == 2
    by_text = {c.text: c for c in summary.claims}
    citing = by_text[s1]
    assert citing.status == "unresolved" and citing.recorded == "provisional"
    assert citing.unpinned == ("pc424242",)
    assert by_text[S2].status == "unresolved"
    # An unpinned citation does not re-queue the memory as need: it stays
    # in the queue only as the baseline starvation-guard slot.
    (candidate,) = g.grounding_audit_candidates(store)
    assert candidate.target_id == rid and candidate.payload["baseline"] is True


def test_moved_source_pin_requeues_dependent_memory(store: Store) -> None:
    """The memory is a dependent consumer of its cited sources: when a
    source changes, the memory leaves the baseline class and re-enters the
    audit queue as stale — a read must not be the only place the staleness
    shows."""
    source = store.insert_ref(kind="quest", slug=None, title="Cited source")
    src_handle = f"qu{source.id}"
    body = f"{S1[:-1]} ({src_handle}). {S2}"
    rid = _memory(store, body)
    _audit(
        store,
        rid,
        body,
        _claim(0, quote=f"{S1[:-1]} ({src_handle}).", evidence=[src_handle]),
        _claim(1, kind="definition"),
    )
    (before,) = g.grounding_audit_candidates(store)
    assert before.target_id == rid and before.payload["baseline"] is True
    # The cited source changes: the memory re-enters the queue as stale.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = 'Cited source v2' WHERE ref_id = %s",
            (int(source.id),),
        )
    (after,) = g.grounding_audit_candidates(store)
    assert after.target_id == rid
    assert after.payload.get("baseline") is False
    assert after.action == "audit"


def test_classifier_coverage_remains_a_lower_bound(store: Store) -> None:
    rid = _memory(store, S1)
    record = _audit(store, rid, S1, _claim(0))
    assert record.coverage["claim_coverage_verified"] is False
    assert record.coverage["claims_total"] is None
    summary = g.grounding_summary(store, rid)
    assert summary.truncated
    assert "1+ claims" in g.summary_line(summary)
    assert "/1+ supported" in g.grounding_chip(store, rid)
    assert "lower bound" in g.grounding_stats_report(store)
