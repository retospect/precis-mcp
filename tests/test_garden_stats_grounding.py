"""The grounding block of ``precis garden stats`` (gr634490 slice 5).

The report prints substantive-claim denominators and scan boundaries from
the audit rows themselves — never reviewed-memory or citation counts
standing in for grounding coverage.
"""

from __future__ import annotations

import json
from typing import Any

from precis.graph.garden import grounding as g
from precis.store import Store
from precis.store.types import ChunkInsert, Tag

S1 = "The ingest worker stores every body chunk before the embedder runs."
S2 = "A chunk is the smallest addressable text unit."
BODY = f"{S1} {S2}"


def _memory(store: Store, title: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=BODY, meta={"chunk_kind": "memory_body"})]
    )
    store.add_tag(ref.id, Tag.open("section:reference"), set_by="system")
    return int(ref.id)


def _reply(*entries: dict[str, Any]) -> g.Call:
    def call(prompt: str) -> tuple[str, str | None]:
        return json.dumps({"claims": list(entries)}), None

    return call


def test_stats_reports_unchecked_not_reviewed_counts(store: Store) -> None:
    audited = _memory(store, "Audited fixture")
    _memory(store, "Never audited fixture")
    sha = store.target_sha("ref", audited)
    assert sha is not None
    # One definition the classifier suggested; the classifier skipped the
    # second span, so one sentence is unchecked — a reviewed-memory count
    # would say "1/1 audited" and hide that. The suggestion is unverified:
    # it reads unresolved, never supported.
    rec = g.audit_body(
        BODY,
        "Audited fixture",
        sha,
        call=_reply(
            {
                "i": 0,
                "quote": S1,
                "kind": "definition",
                "status": "supported",
                "evidence": [],
            }
        ),
    )
    assert g.write_audit(store, audited, rec) is not None

    report = g.grounding_stats_report(store)
    assert "1 memories audited · 1 never audited" in report
    assert (
        "claims: 2+ substantive (lower bound — 1 incomplete scans)"
        " — 0 supported · 0 partial · 0 unsupported"
        " · 0 contradicted · 1 unresolved · 1 unchecked" in report
    )
    assert "inspected: 1 claims judged · unchecked scope: 1 claims" in report
    assert "1 current · 0 stale" in report
    assert "scan bound: 60 claims / 16000 chars per memory" in report

    # An edit stales the audit: its claims stop certifying support and the
    # denominator stays claim-level.
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = 'Edited' WHERE ref_id = %s", (audited,))
    report = g.grounding_stats_report(store)
    assert (
        "0 supported · 0 partial · 0 unsupported · 0 contradicted"
        " · 2 unresolved · 0 unchecked" in report
    )
    assert "0 current · 1 stale" in report


def test_stats_empty(store: Store) -> None:
    report = g.grounding_stats_report(store)
    assert "0 memories audited" in report
    assert (
        "claims: 0 substantive — 0 supported · 0 partial · 0 unsupported"
        " · 0 contradicted · 0 unresolved · 0 unchecked" in report
    )


def test_stats_unpinned_citations_and_moved_pins(store: Store) -> None:
    """The stats block applies the same read-side rules as the summary: a
    citation that never resolved counts unresolved, never supported."""
    rid = _memory(store, "Cited fixture")
    sha = store.target_sha("ref", rid)
    assert sha is not None
    body = f"{S1[:-1]} (pc424242). {S2}"
    rec = g.audit_body(
        body,
        "Cited fixture",
        sha,
        call=_reply(
            {
                "i": 0,
                "quote": f"{S1[:-1]} (pc424242).",
                "kind": "empirical",
                "status": "supported",
                "evidence": ["pc424242"],
            },
            {
                "i": 1,
                "quote": S2,
                "kind": "definition",
                "status": "supported",
                "evidence": [],
            },
        ),
    )
    assert g.write_audit(store, rid, rec) is not None

    report = g.grounding_stats_report(store)
    # The unresolvable citation certifies nothing and the bare definition
    # beside it is a classifier-only suggestion: 0 supported, 2 unresolved.
    assert (
        "claims: 2+ substantive (lower bound — 1 incomplete scans) — 0 supported · 0 partial · 0 unsupported"
        " · 0 contradicted · 2 unresolved · 0 unchecked" in report
    )
    assert "1 current · 0 stale" in report

    # An edit stales the row: no claim certifies support any more.
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = 'Edited' WHERE ref_id = %s", (rid,))
    report = g.grounding_stats_report(store)
    assert (
        "0 supported · 0 partial · 0 unsupported · 0 contradicted"
        " · 2 unresolved · 0 unchecked" in report
    )
    assert "0 current · 1 stale" in report


def test_stats_moved_source_counts_only_citing_claims(store: Store) -> None:
    """A partially stale audit (one cited source moved) keeps per-claim
    coverage: exactly the citing claim loses support, an unaffected
    supported claim keeps it, and a previously unchecked claim stays
    unchecked — out of the inspected count (gr634490)."""
    paper = store.insert_ref(kind="paper", slug="source-paper", title="Source paper")
    store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="passage text")])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (int(paper.id),)
        ).fetchone()
    assert row is not None
    handle = f"pc{int(row[0])}"
    body = f"{S1[:-1]} ({handle}). {S2}"
    ref = store.insert_ref(kind="memory", slug=None, title="Cited fixture")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    store.add_tag(ref.id, Tag.open("section:reference"), set_by="system")
    sha = store.target_sha("ref", int(ref.id))
    assert sha is not None
    rec = g.audit_body(
        body,
        "Cited fixture",
        sha,
        call=_reply(
            {
                "i": 0,
                "quote": f"{S1[:-1]} ({handle}).",
                "kind": "empirical",
                "status": "supported",
                "evidence": [handle],
            }
        ),
    )
    # The pipeline shape of a passage-verified claim: rollup recorded.
    rec.claims[0].rollup = "supported"
    assert g.write_audit(store, int(ref.id), rec) is not None

    report = g.grounding_stats_report(store)
    assert (
        "claims: 2+ substantive (lower bound — 1 incomplete scans)"
        " — 1 supported · 0 partial · 0 unsupported"
        " · 0 contradicted · 0 unresolved · 1 unchecked" in report
    )
    assert "inspected: 1 claims judged · unchecked scope: 1 claims" in report
    assert "1 current · 0 stale" in report

    # The cited source moves: exactly the citing claim loses support; the
    # unchecked sentence stays unchecked — and stays out of inspected.
    store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=1, text="added later")])
    report = g.grounding_stats_report(store)
    assert (
        "0 supported · 0 partial · 0 unsupported · 0 contradicted"
        " · 1 unresolved · 1 unchecked" in report
    )
    assert "inspected: 1 claims judged · unchecked scope: 1 claims" in report
    assert "0 current · 1 stale" in report
