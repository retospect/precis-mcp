"""Directed claim minting (``precis.taproot.directed``) —
docs/backlog/taproot-directed-claim-minting.md.

Two layers, mirroring ``tests/test_taproot_backfill.py``'s split:

* :func:`qualify_claim` parsing — ``directed.dispatch`` monkeypatched (no live
  LLM anywhere in this file), exercising the JSON contract, the quote-not-
  found anti-hallucination check, and the strict dispatch posture
  (:class:`QualifyUnavailable` on a dispatch error).
* :func:`directed_mint` — DB-backed via the ``store`` fixture, with
  ``qualify_fn``/``block_fn``/``judge_fn``/``merge_confirm_fn`` all injected
  so convergence is deterministic and no LLM/embedder runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.errors import BadInput
from precis.store.store import Store
from precis.taproot import directed
from precis.taproot.canon import Candidate, CanonicalClaim, Verdict, claim_sha
from precis.taproot.directed import (
    DirectedMintReport,
    QualifyResult,
    QualifyUnavailable,
    directed_mint,
    qualify_claim,
    render_report,
)
from precis.taproot.hub import mint_hub
from tests.workers._helpers import seed_chunk, seed_ref

_PASSAGE = (
    "Pd/C catalyzes Suzuki coupling of aryl bromides at room temperature "
    "with K2CO3 in aqueous ethanol, giving >90% yield."
)


def _result(
    *, data: dict[str, Any] | None = None, text: str = "", error: str | None = None
) -> Any:
    """A stand-in for ``LlmResult`` — ``qualify_claim`` only reads ``.error``,
    ``.data``, and ``.text`` (mirrors ``test_taproot_canon.py``'s ``_result``)."""
    return SimpleNamespace(text=text, data=data, error=error)


def _verdict(v: str, c: float, rationale: str = "test") -> Verdict:
    return {"verdict": v, "confidence": c, "rationale": rationale}  # type: ignore[typeddict-item]


# ── qualify_claim ─────────────────────────────────────────────────────────


def test_qualify_supported_returns_qualified_claim_and_verified_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        directed,
        "dispatch",
        lambda req: _result(
            data={
                "supported": True,
                "claim": "Pd/C catalyzes Suzuki coupling of aryl bromides at RT.",
                "method": "Suzuki coupling",
                "regime": "RT",
                "quote": "Pd/C catalyzes Suzuki coupling of aryl bromides at "
                "room temperature",
                "reason": "narrowed to aryl bromides + RT, the passage's exact scope",
            }
        ),
    )

    result = qualify_claim("Pd/C catalyzes Suzuki coupling.", _PASSAGE)

    assert result.supported is True
    assert result.claim is not None
    assert result.claim.sentence == (
        "Pd/C catalyzes Suzuki coupling of aryl bromides at RT."
    )
    assert result.claim.scope == {"method": "Suzuki coupling", "regime": "RT"}
    assert result.quote == (
        "Pd/C catalyzes Suzuki coupling of aryl bromides at room temperature"
    )
    assert "narrowed" in result.reason


def test_qualify_unsupported_returns_reason_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        directed,
        "dispatch",
        lambda req: _result(
            data={
                "supported": False,
                "claim": None,
                "quote": None,
                "reason": "the passage never mentions palladium at all",
            }
        ),
    )

    result = qualify_claim("Pd/C catalyzes Suzuki coupling.", "Unrelated passage.")

    assert result.supported is False
    assert result.claim is None
    assert result.quote is None
    assert result.reason == "the passage never mentions palladium at all"


def test_qualify_quote_not_in_passage_invalidates_to_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model claims ``supported=True`` but the quote it hands back is
    fabricated (not a verbatim substring of the passage, even after
    whitespace collapse) — the anti-hallucination backstop must override the
    model's own verdict."""
    monkeypatch.setattr(
        directed,
        "dispatch",
        lambda req: _result(
            data={
                "supported": True,
                "claim": "Pd/C catalyzes Suzuki coupling at RT.",
                "quote": "this sentence does not appear in the passage at all",
                "reason": "should be overridden",
            }
        ),
    )

    result = qualify_claim("Pd/C catalyzes Suzuki coupling.", _PASSAGE)

    assert result.supported is False
    assert result.claim is None
    assert result.quote is None
    assert result.reason == "quote not found in passage"


def test_qualify_quote_matches_after_whitespace_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quote that differs only in whitespace/line-wrapping from the
    passage still verifies — the check is whitespace-collapsed, not
    byte-exact."""
    wrapped_passage = "Pd/C catalyzes  Suzuki\ncoupling  at room temperature."
    monkeypatch.setattr(
        directed,
        "dispatch",
        lambda req: _result(
            data={
                "supported": True,
                "claim": "Pd/C catalyzes Suzuki coupling at RT.",
                "quote": "Pd/C catalyzes Suzuki coupling at room temperature.",
                "reason": "ok",
            }
        ),
    )

    result = qualify_claim("Pd/C catalyzes Suzuki coupling.", wrapped_passage)

    assert result.supported is True
    assert result.quote == "Pd/C catalyzes Suzuki coupling at room temperature."


def test_qualify_supported_with_no_quote_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        directed,
        "dispatch",
        lambda req: _result(
            data={
                "supported": True,
                "claim": "Pd/C catalyzes Suzuki coupling.",
                "quote": None,
                "reason": "ok",
            }
        ),
    )

    result = qualify_claim("Pd/C catalyzes Suzuki coupling.", _PASSAGE)

    assert result.supported is False
    assert "grounding quote" in result.reason


def test_qualify_unparseable_response_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model responded (no dispatch error) but the text isn't JSON —
    a semantic parse failure, not an infra failure: degrades to unsupported,
    never raises."""
    monkeypatch.setattr(
        directed, "dispatch", lambda req: _result(text="not json at all, sorry")
    )

    result = qualify_claim("Pd/C catalyzes Suzuki coupling.", _PASSAGE)

    assert result.supported is False
    assert result.claim is None
    assert result.reason == "unparseable model output"


def test_qualify_dispatch_error_raises_unavailable_never_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict posture: an LLM outage must surface as infra failure, not
    read as 'the passage doesn't support this'."""
    monkeypatch.setattr(directed, "dispatch", lambda req: _result(error="ECONNREFUSED"))

    with pytest.raises(QualifyUnavailable, match="ECONNREFUSED"):
        qualify_claim("Pd/C catalyzes Suzuki coupling.", _PASSAGE)


def test_qualify_empty_proposed_or_passage_short_circuits_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(req: Any) -> Any:
        raise AssertionError("dispatch should not have been called")

    monkeypatch.setattr(directed, "dispatch", _boom)

    assert qualify_claim("", _PASSAGE).supported is False
    assert qualify_claim("a claim", "   ").supported is False


# ── directed_mint — DB-backed, fake qualify/cascade fns ─────────────────


def _qualify_ok(
    sentence: str,
    quote: str,
    *,
    scope: dict[str, str] | None = None,
    reason: str = "qualified",
) -> Any:
    def _fn(proposed: str, passage: str) -> QualifyResult:
        return QualifyResult(
            supported=True,
            claim=CanonicalClaim(sentence=sentence, scope=scope or {}),
            quote=quote,
            reason=reason,
        )

    return _fn


def _qualify_no(reason: str = "not supported") -> Any:
    def _fn(proposed: str, passage: str) -> QualifyResult:
        return QualifyResult(supported=False, claim=None, quote=None, reason=reason)

    return _fn


def _block_none(claim: CanonicalClaim, store: Any, embedder: Any) -> list[Candidate]:
    return []


def _block_hit(hub_ref_id: int, claim_text: str) -> Any:
    def _b(claim: CanonicalClaim, store: Any, embedder: Any) -> list[Candidate]:
        return [Candidate(hub_ref_id=hub_ref_id, claim=claim_text, distance=0.02)]

    return _b


def _never_called(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("cascade fn should not have been called")


def _paper_chunk(
    store: Store, *, text: str = _PASSAGE, title: str = "src paper"
) -> tuple[int, int]:
    """A paper + one live body chunk on it; return (paper_ref_id, chunk_id)."""
    paper = seed_ref(store, title=title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=text)
    return paper, chunk_id


def _table_counts(store: Store) -> dict[str, int]:
    counts: dict[str, int] = {}
    with store.pool.connection() as conn:
        for table in ("refs", "chunks", "links", "ref_tags"):
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None
            counts[table] = int(row[0])
    return counts


def _hub_meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _link_meta(store: Store, *, src: int, dst: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


# ── _read_passage_chunk guard ────────────────────────────────────────────


def test_directed_mint_rejects_non_evidence_source_chunk(store: Store) -> None:
    draft = seed_ref(store, title="not a paper", kind="todo")
    chunk_id = seed_chunk(store, ref_id=draft, text="some text")

    with pytest.raises(BadInput):
        directed_mint(
            store,
            embedder=None,
            proposed="anything",
            chunk_id=chunk_id,
            qualify_fn=_never_called,
        )


def test_directed_mint_rejects_unknown_chunk(store: Store) -> None:
    with pytest.raises(BadInput):
        directed_mint(
            store,
            embedder=None,
            proposed="anything",
            chunk_id=999_999_999,
            qualify_fn=_never_called,
        )


# ── unsupported qualify → cascade never runs, zero writes ──────────────


def test_directed_mint_unsupported_skips_cascade_and_writes_nothing(
    store: Store,
) -> None:
    _, chunk_id = _paper_chunk(store)
    before = _table_counts(store)

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes an unrelated reaction.",
        chunk_id=chunk_id,
        qualify_fn=_qualify_no("the passage never discusses this reaction"),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )

    assert report.qualify.supported is False
    assert report.placement is None
    assert report.hub_ref_id is None
    assert report.applied is False
    assert _table_counts(store) == before

    # Also true when the caller asked for --apply: an unsupported qualify
    # never reaches a write door regardless of the flag.
    report2 = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes an unrelated reaction.",
        chunk_id=chunk_id,
        apply=True,
        qualify_fn=_qualify_no("still unsupported"),
        block_fn=_never_called,
        judge_fn=_never_called,
        merge_confirm_fn=_never_called,
    )
    assert report2.applied is False
    assert _table_counts(store) == before


# ── dry-run (apply=False, the default) — plan only, zero writes ────────


def test_directed_mint_dry_run_new_makes_no_writes(store: Store) -> None:
    _, chunk_id = _paper_chunk(store)
    before = _table_counts(store)

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling of aryl bromides at RT.",
            "Pd/C catalyzes Suzuki coupling of aryl bromides at room temperature",
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
    )

    assert report.qualify.supported is True
    assert report.placement is not None
    assert report.placement.action == "new"
    assert report.hub_ref_id is None  # not minted yet — dry run
    assert report.applied is False
    assert _table_counts(store) == before


def test_directed_mint_dry_run_attach_reports_target_hub_no_writes(
    store: Store,
) -> None:
    existing = mint_hub(
        store,
        CanonicalClaim(sentence="Pd/C catalyzes Suzuki coupling at RT.", scope={}),
    )
    _, chunk_id = _paper_chunk(store)
    before = _table_counts(store)

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling at RT.", "Pd/C catalyzes Suzuki coupling"
        ),
        block_fn=_block_hit(existing, "Pd/C catalyzes Suzuki coupling at RT."),
        judge_fn=lambda a, b: _verdict("same", 0.95),
    )

    assert report.placement is not None
    assert report.placement.action == "attach"
    assert report.hub_ref_id == existing  # known at plan time for attach
    assert report.applied is False
    assert _table_counts(store) == before


# ── apply=True — through the real write doors ───────────────────────────


def test_directed_mint_apply_new_mints_hub_and_attaches_evidence_with_quote(
    store: Store,
) -> None:
    paper, chunk_id = _paper_chunk(store)
    quote = "Pd/C catalyzes Suzuki coupling of aryl bromides at room temperature"

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        demand="qu123456",
        apply=True,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling of aryl bromides at RT.", quote
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
    )

    assert report.applied is True
    assert report.hub_ref_id is not None
    hub_id = report.hub_ref_id

    with store.pool.connection() as conn:
        kind = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (hub_id,)
        ).fetchone()
    assert kind is not None and kind[0] == "finding"

    meta = _hub_meta(store, hub_id)
    assert meta.get("demanded_by") == ["qu123456"]

    edge_meta = _link_meta(store, src=paper, dst=hub_id)
    assert edge_meta.get("quote") == quote
    assert edge_meta.get("origin") == "directed-mint"
    assert edge_meta.get("source_handle") == f"pc{chunk_id}"
    # Qualify IS a claim-vs-passage verification, so the edge is born
    # verified — the full six-key verdict shape, sha-bound to the
    # qualified sentence.
    assert edge_meta["support"] == "yes"
    assert edge_meta["support_reason"] == "qualified"
    assert edge_meta["caveats"] == []
    assert edge_meta["verified_by"] == "directed-mint"
    assert edge_meta["verified_at"]
    assert edge_meta["verified_claim_sha"] == claim_sha(
        "Pd/C catalyzes Suzuki coupling of aryl bromides at RT."
    )

    # Grounded at the specific passage, not ref-level.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT src_chunk_id FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper, hub_id),
        ).fetchone()
    assert row is not None and row[0] is not None


def test_directed_mint_apply_attach_converges_and_stamps_demand_on_existing_hub(
    store: Store,
) -> None:
    existing = mint_hub(
        store,
        CanonicalClaim(sentence="Pd/C catalyzes Suzuki coupling at RT.", scope={}),
    )
    paper, chunk_id = _paper_chunk(store)

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        demand="dr654321",
        apply=True,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling at RT.", "Pd/C catalyzes Suzuki coupling"
        ),
        block_fn=_block_hit(existing, "Pd/C catalyzes Suzuki coupling at RT."),
        judge_fn=lambda a, b: _verdict("same", 0.95),
    )

    assert report.applied is True
    assert report.hub_ref_id == existing

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'finding'"
        ).fetchone()
    assert row is not None and row[0] == 1  # converged, no second hub minted

    assert _hub_meta(store, existing).get("demanded_by") == ["dr654321"]
    edge_meta = _link_meta(store, src=paper, dst=existing)
    assert edge_meta.get("origin") == "directed-mint"


def test_directed_mint_demand_stamp_accumulates_across_demanders(
    store: Store,
) -> None:
    """A second directed mint attaching to the same hub appends its demand
    — provenance of every demander survives (list semantics, no
    last-writer-wins)."""
    existing = mint_hub(
        store,
        CanonicalClaim(sentence="Pd/C catalyzes Suzuki coupling at RT.", scope={}),
    )
    _, chunk_id = _paper_chunk(store)

    for demand in ("qu111111", "dr222222", "qu111111"):  # third is a dup
        directed_mint(
            store,
            embedder=None,
            proposed="Pd/C catalyzes Suzuki coupling.",
            chunk_id=chunk_id,
            demand=demand,
            apply=True,
            qualify_fn=_qualify_ok(
                "Pd/C catalyzes Suzuki coupling at RT.",
                "Pd/C catalyzes Suzuki coupling",
            ),
            block_fn=_block_hit(existing, "Pd/C catalyzes Suzuki coupling at RT."),
            judge_fn=lambda a, b: _verdict("same", 0.95),
        )

    assert _hub_meta(store, existing).get("demanded_by") == ["qu111111", "dr222222"]


def test_directed_mint_apply_false_performs_no_store_writes(store: Store) -> None:
    """The row-count check, mirroring
    ``test_taproot_migrate.py::test_dry_run_performs_no_store_writes``:
    ``apply=False`` (the default) never touches ``refs``/``chunks``/
    ``links``/``ref_tags``, even for a claim the qualify step supports and
    the cascade would place as ``new``."""
    _, chunk_id = _paper_chunk(store)
    before = _table_counts(store)

    directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        demand="qu999999",
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling at RT.", "Pd/C catalyzes Suzuki coupling"
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
    )

    assert _table_counts(store) == before


def test_directed_mint_needs_review_files_todo_and_mints_no_hub(store: Store) -> None:
    existing = mint_hub(
        store,
        CanonicalClaim(sentence="Pd/C catalyzes Suzuki coupling at RT.", scope={}),
    )
    _, chunk_id = _paper_chunk(store)

    def _todos(s: Store) -> int:
        with s.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'todo' "
                "AND title LIKE 'taproot: review directed-mint%'"
            ).fetchone()
        return int(row[0]) if row else 0

    before_todos = _todos(store)
    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        demand="qu777777",
        apply=True,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling, maybe at RT.",
            "Pd/C catalyzes Suzuki coupling",
        ),
        block_fn=_block_hit(existing, "Pd/C catalyzes Suzuki coupling at RT."),
        judge_fn=lambda a, b: _verdict("same", 0.40),  # low-confidence same
        merge_confirm_fn=lambda a, b: _verdict("different", 0.9),  # not confirmed
    )

    assert report.placement is not None
    assert report.placement.action == "needs_review"
    assert report.hub_ref_id is None
    assert _todos(store) == before_todos + 1


def test_directed_mint_needs_review_uses_injected_todo_fn(store: Store) -> None:
    existing = mint_hub(
        store,
        CanonicalClaim(sentence="Pd/C catalyzes Suzuki coupling at RT.", scope={}),
    )
    _, chunk_id = _paper_chunk(store)
    calls: list[str] = []

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        apply=True,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling, maybe at RT.",
            "Pd/C catalyzes Suzuki coupling",
        ),
        block_fn=_block_hit(existing, "Pd/C catalyzes Suzuki coupling at RT."),
        judge_fn=lambda a, b: _verdict("same", 0.40),
        merge_confirm_fn=lambda a, b: _verdict("different", 0.9),
        todo_fn=lambda claim, placement: calls.append(claim.sentence),
    )

    assert report.placement is not None and report.placement.action == "needs_review"
    assert calls == ["Pd/C catalyzes Suzuki coupling, maybe at RT."]


# ── render_report — smoke test ──────────────────────────────────────────


def test_render_report_unsupported() -> None:
    report = DirectedMintReport(
        proposed="X does Y.",
        chunk_id=42,
        passage_ref_id=1,
        passage_ref_kind="paper",
        passage_ref_title="some paper",
        demand=None,
        qualify=QualifyResult(
            supported=False, claim=None, quote=None, reason="not in passage"
        ),
    )
    rendered = render_report(report)
    assert "UNSUPPORTED" in rendered
    assert "not in passage" in rendered
    assert "X does Y." in rendered


def test_render_report_applied_new(store: Store) -> None:
    paper, chunk_id = _paper_chunk(store)
    quote = "Pd/C catalyzes Suzuki coupling of aryl bromides at room temperature"

    report = directed_mint(
        store,
        embedder=None,
        proposed="Pd/C catalyzes Suzuki coupling.",
        chunk_id=chunk_id,
        demand="qu222222",
        apply=True,
        qualify_fn=_qualify_ok(
            "Pd/C catalyzes Suzuki coupling of aryl bromides at RT.", quote
        ),
        block_fn=_block_none,
        judge_fn=_never_called,
    )
    rendered = render_report(report)

    assert "Qualified claim" in rendered
    assert quote in rendered
    assert "qu222222" in rendered
    assert f"fi{report.hub_ref_id}" in rendered
    assert "Applied" in rendered
