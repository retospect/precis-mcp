"""Pure-shape units for the claim page's maturity ladder + gates report
(``precis_web.nanopub_render._ladder`` / ``_gate_report``) and the ask-box
model label — written to kill the 2026-08-27 mutation survivors: the
route-level tests render the HTML but never asserted which rung is
current/done, which group a status came from, or the label fallback.

The trailing ``_dispute_panel``/``_contradicted_panel`` section is
DB-backed (D1, docs/backlog/disputes-edge-nonblocking-disagreement.md) —
those two read live ``links`` rows, so a fake row can't stand in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis_web.nanopub_render import (
    _contradicted_panel,
    _dispute_panel,
    _gate_report,
    _ladder,
)


@dataclass
class _Row:
    updated_at: datetime | None


@dataclass
class _Issue:
    check: str
    message: str
    blocking: bool = True


def test_ladder_unminted_lights_nothing() -> None:
    steps = _ladder(None, None, disputed=False)
    assert [s["name"] for s in steps] == [
        "candidate",
        "reviewed",
        "signed",
        "anchored",
        "published",
    ]
    assert not any(s["done"] for s in steps)
    assert not any(s["current"] for s in steps)
    assert not any(s["blocked"] for s in steps)


def test_ladder_reviewed_marks_climbed_rungs_and_one_current() -> None:
    row = _Row(updated_at=datetime(2026, 8, 27, 10, 30, tzinfo=UTC))
    steps = {s["name"]: s for s in _ladder("reviewed", row, disputed=False)}
    assert steps["candidate"]["done"] and not steps["candidate"]["current"]
    assert steps["reviewed"]["done"] and steps["reviewed"]["current"]
    assert not steps["signed"]["done"] and not steps["signed"]["current"]
    assert not steps["published"]["done"]
    # The since-timestamp rides ONLY the current rung's tip.
    assert "In this state since 2026-08-27 10:30Z" in steps["reviewed"]["tip"]
    assert not any(
        "In this state since" in s["tip"] for n, s in steps.items() if n != "reviewed"
    )
    assert not any(s["blocked"] for s in steps.values())


def test_ladder_dispute_blocks_only_the_current_rung() -> None:
    steps = {s["name"]: s for s in _ladder("signed", _Row(None), disputed=True)}
    assert steps["signed"]["blocked"]
    assert "BLOCKED" in steps["signed"]["tip"]
    assert not any(s["blocked"] for n, s in steps.items() if n != "signed")


def test_gate_report_unminted_is_all_pending() -> None:
    report = _gate_report(None, [])
    assert report["mint"] and report["preflight"]
    assert all(g["status"] == "pending" for g in report["mint"])
    assert all(g["status"] == "pending" for g in report["preflight"])


def test_gate_report_candidate_mixes_live_issues_with_pending() -> None:
    report = _gate_report("candidate", [_Issue("state", "not anchored", blocking=True)])
    pre = {g["name"]: g for g in report["preflight"]}
    assert all(g["status"] == "pending" for g in report["mint"])
    assert pre["state"]["status"] == "failed"
    assert pre["state"]["message"] == "not anchored"
    assert pre["withheld-edge"]["status"] == "pending"


def test_gate_report_reviewed_mint_passed_preflight_reads_live_issues() -> None:
    report = _gate_report(
        "reviewed",
        [
            _Issue("state", "state is 'reviewed', not 'anchored'", blocking=True),
            _Issue("ots-pending", "calendar-pending", blocking=False),
        ],
    )
    # Approve refuses on any mint-gate violation, so state ≥ reviewed
    # means every mint gate passed — mechanically, all of them.
    assert all(g["status"] == "passed" for g in report["mint"])
    pre = {g["name"]: g for g in report["preflight"]}
    assert pre["state"]["status"] == "failed"
    assert pre["ots-pending"]["status"] == "note"
    # A check with no live issue at reviewed+ reads passed, not pending.
    assert pre["withheld-edge"]["status"] == "passed"
    assert pre["trust"]["status"] == "passed"


def test_gate_report_dryrun_gives_live_per_gate_standing() -> None:
    """Pre-approve, a dry-run replaces blanket "pending": violated gates
    read failed with their message, clean ones passed, the claim-sentence
    row carries the advisory lints as a note ("passing, with
    considerations"), and an off-vocabulary violation slug is appended
    rather than hidden."""
    report = _gate_report(
        "candidate",
        [],
        dryrun={
            "violations": {
                "grounding": ["passage 1 has no DOI", "passage 2 has no quote"],
                "brand-new-gate": ["something novel broke"],
            },
            "advisories": [
                "scope-material: name the material",
                "tilde-approximation: use ≈",
            ],
        },
    )
    assert report["dryrun"] is True
    mint = {g["name"]: g for g in report["mint"]}
    assert mint["grounding"]["status"] == "failed"
    assert mint["grounding"]["message"] == "passage 1 has no DOI (+1 more)"
    assert mint["contradicts"]["status"] == "passed"
    assert mint["claim-sentence"]["status"] == "note"
    assert "scope-material, tilde-approximation" in mint["claim-sentence"]["message"]
    assert mint["brand-new-gate"]["status"] == "failed"
    # Preflight stays pending — no publish row yet at candidate.
    assert all(g["status"] == "pending" for g in report["preflight"])


def test_gate_report_dryrun_clean_is_all_passing() -> None:
    report = _gate_report("candidate", [], dryrun={"violations": {}, "advisories": []})
    assert report["dryrun"] is True
    assert all(g["status"] == "passed" for g in report["mint"])


def test_gate_report_minted_ignores_dryrun_and_unparseable_prefill_degrades() -> None:
    # A reviewed row's gates passed for real — a stray dry-run must not
    # relabel them; and pre-approve WITHOUT a dry-run (unparseable
    # prefill) degrades to pending, never crashes.
    minted = _gate_report(
        "reviewed", [], dryrun={"violations": {"grounding": ["x"]}, "advisories": []}
    )
    assert minted["dryrun"] is False
    assert all(g["status"] == "passed" for g in minted["mint"])
    degraded = _gate_report("candidate", [], dryrun=None)
    assert degraded["dryrun"] is False
    assert all(g["status"] == "pending" for g in degraded["mint"])


# ── ``_dispute_panel`` / ``_contradicted_panel`` (D1) ───────────────────


def test_dispute_panel_returns_disputes_edge_entries_with_counterpart_info(
    store: Any,
) -> None:
    """The non-blocking open-questions panel: one entry per live
    `disputes` edge, either direction, naming the counterpart — and
    resolving the disputing passage's text when the edge names a
    chunk."""
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub
    from tests.workers._helpers import seed_ref

    hub = mint_hub(store, CanonicalClaim(sentence="a panel-tested claim", scope={}))
    disputer = seed_ref(store, title="a disputing finding", kind="finding")
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', 0, 'finding_body', %s) RETURNING chunk_id",
            (disputer, "This claim looks wrong because of X."),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
        conn.commit()
    store.add_link(
        src_ref_id=disputer,
        dst_ref_id=hub,
        src_pos=0,
        relation="disputes",
    )

    entries = _dispute_panel(store, hub)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["ref_id"] == disputer
    assert entry["kind"] == "finding"
    assert entry["direction"] == "in"
    assert entry["passage"] == "This claim looks wrong because of X."
    assert chunk_id  # sanity: the pinned chunk really was resolved


def test_dispute_panel_resolves_passage_from_meta_source_handle(store: Any) -> None:
    """The production writers (``workers/hub_refine.py``) set NO chunk
    column on the edge — their pointer is ``links.meta['source_handle']``
    (``pc<id>``), same as evidence edges. The panel must resolve the
    passage from that fallback, or every automatically-filed dispute
    renders with an empty passage (pre-ship review finding #2)."""
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub
    from tests.workers._helpers import seed_ref

    hub = mint_hub(store, CanonicalClaim(sentence="a handle-pinned claim", scope={}))
    paper = seed_ref(store, title="a disputing paper", kind="paper")
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', 0, 'paragraph', %s) RETURNING chunk_id",
            (paper, "The measured modulus runs counter to the claim."),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
        conn.commit()
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation="disputes",
        meta={"source_handle": f"pc{chunk_id}", "dialectic": None},
    )

    entries = _dispute_panel(store, hub)

    assert len(entries) == 1
    assert entries[0]["ref_id"] == paper
    assert entries[0]["direction"] == "in"
    assert entries[0]["passage"] == "The measured modulus runs counter to the claim."


def test_dispute_panel_empty_when_no_live_disputes_edge(store: Any) -> None:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    hub = mint_hub(store, CanonicalClaim(sentence="an undisputed claim", scope={}))

    assert _dispute_panel(store, hub) == []


def test_contradicted_panel_names_the_counterpart() -> None:
    """Just enough to name it (kind, ref_id, title, direction) — the
    blocking banner exists to say "adjudicate this," not to relitigate."""
    from precis.nanopub.evidence import ContradictsEdge

    edges = [
        ContradictsEdge(
            ref_id=42, kind="finding", title="a review critique", direction="in"
        )
    ]
    assert _contradicted_panel(edges) == [
        {
            "ref_id": 42,
            "kind": "finding",
            "title": "a review critique",
            "direction": "in",
        }
    ]


def test_answer_model_label_env_chain(monkeypatch) -> None:
    from precis_web.ask import answer_model_label

    monkeypatch.delenv("PRECIS_FOLLOWUP_MODEL", raising=False)
    monkeypatch.delenv("PRECIS_DREAM_AGENT_MODEL", raising=False)
    assert answer_model_label() == "sonnet"
    monkeypatch.setenv("PRECIS_DREAM_AGENT_MODEL", "haiku")
    assert answer_model_label() == "haiku"
    monkeypatch.setenv("PRECIS_FOLLOWUP_MODEL", "opus")
    assert answer_model_label() == "opus"
