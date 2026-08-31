"""Verifying withheld/unverified evidence edges
(`src/precis/taproot/verify_edges.py` + `precis taproot verify-edges`).

DB-backed (real `refs`/`chunks`/`links` via the `store` fixture, hubs minted
through the real `mint_hub` write door so the strict-hub tag predicate is
exercised for real) but never networked: every test monkeypatches the
module-level `_verify_support_with_caveats` hook, so no LLM dispatch runs.

The load-bearing assertions are on the edge's `meta`: a stamp is a jsonb
MERGE onto the original row (unrelated keys survive, no second edge
appears), a non-corroborating verdict never lands a stamp, and a strip
removes exactly the `support` key (the edge returns to withheld behind the
publish gate).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

import pytest

from precis.taproot import verify_edges
from precis.taproot.canon import CanonicalClaim, claim_sha
from precis.taproot.hub import mint_hub
from precis.taproot.verify_edges import (
    count_passageless_edges,
    select_unverified_stamped_edges,
    select_withheld_edges,
    verify_edge,
)
from tests.conftest import _active_dsn
from tests.workers._helpers import seed_chunk, seed_ref

_CLAIM = "Graphene exhibits a tensile strength of 130 GPa."
_PASSAGE = "We measured that graphene exhibits a tensile strength of 130 GPa."
#: Distinct claims -- mint_hub converges on the claim sha, so two seeds
#: sharing a sentence land on ONE hub.
_OTHER_CLAIM = "Silicon carbide sublimes at 2700 degrees Celsius."
_THIRD_CLAIM = "Anatase converts to rutile above 600 degrees Celsius."

#: The born-released stamp: `support` written at mint time, no
#: support_reason, no verified_by -- nobody ever read the passage.
_BORN_RELEASED_META: dict[str, Any] = {"caveats": [], "support": "yes"}

_VERDICT_YES: dict[str, Any] = {
    "supports": "yes",
    "support_reason": "the chunk states the claim directly",
    "caveats": [],
    "contradicts": False,
    "cited_others": [],
    "terminal": True,
}
_VERDICT_PARTIAL: dict[str, Any] = {
    "supports": "partial",
    "support_reason": "supports under a narrower regime",
    "caveats": ["only tested at room temperature"],
    "contradicts": False,
    "cited_others": [],
    "terminal": True,
}
_VERDICT_NO: dict[str, Any] = {
    "supports": "no",
    "support_reason": "the chunk tests something else",
    "caveats": [],
    "contradicts": False,
    "cited_others": [],
    "terminal": True,
}


# ── seeding helpers ──────────────────────────────────────────────────────


def _seed_edge(
    store: Any,
    *,
    claim: str = _CLAIM,
    passage: str = _PASSAGE,
    meta: dict[str, Any] | None = None,
    pinned: bool = True,
    relation: str = "corroborates",
) -> tuple[int, int, int, int]:
    """A hub + a source paper with one body chunk + one inbound evidence
    edge. ``meta=None`` seeds the withheld shape (no ``support`` key);
    ``pinned=False`` leaves ``src_chunk_id`` NULL (the passage-less shape).
    Returns ``(hub_ref_id, paper_ref_id, chunk_id, link_id)``."""
    paper = seed_ref(store, title="Lee 2008", kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text=passage, ord=0)
    hub = mint_hub(store, CanonicalClaim(sentence=claim, scope={}))
    link = store.add_link(
        src_ref_id=paper,
        dst_ref_id=hub,
        relation=relation,
        src_pos=0 if pinned else None,
        meta={"source_handle": f"pc{chunk_id}", **(meta or {})},
    )
    return hub, paper, chunk_id, int(link.id)


def _link_meta(store: Any, link_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM links WHERE link_id = %s", (link_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _link_count(store: Any) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM links").fetchone()
    assert row is not None
    return int(row[0])


def _patch_verify(
    monkeypatch: Any,
    verdict: dict[str, Any] | None,
    *,
    calls: list[dict[str, Any]] | None = None,
) -> None:
    """Bind the module-level `_verify_support_with_caveats` the sweep calls
    -- no network, no router. ``calls`` (when given) records what the hook
    received, so a test can assert the pinned passage + cite info flowed."""

    def _fake(
        *,
        claim: str,
        scope: dict[str, Any],
        target_cite_key: str,
        target_chunk_ord: int,
        target_chunk_text: str,
        source_kind: str = "paper",
    ) -> dict[str, Any] | None:
        if calls is not None:
            calls.append(
                {
                    "claim": claim,
                    "scope": scope,
                    "cite_key": target_cite_key,
                    "ord": target_chunk_ord,
                    "chunk_text": target_chunk_text,
                    "source_kind": source_kind,
                }
            )
        return dict(verdict) if verdict is not None else None

    monkeypatch.setattr(verify_edges, "_verify_support_with_caveats", _fake)


def _never_verify(monkeypatch: Any) -> None:
    def _boom(**_kwargs: Any) -> dict[str, Any] | None:
        raise AssertionError("the verify hook must not have been called")

    monkeypatch.setattr(verify_edges, "_verify_support_with_caveats", _boom)


# ── cohort selection ─────────────────────────────────────────────────────


def test_withheld_cohort_selects_the_pinned_unstamped_edge(store: Any) -> None:
    hub, paper, chunk_id, link_id = _seed_edge(store)

    (edge,) = select_withheld_edges(store)

    # Every field maps -- a transposed column would verify the wrong text
    # or stamp the wrong row.
    assert (edge.link_id, edge.hub_ref_id, edge.source_ref_id) == (link_id, hub, paper)
    assert edge.chunk_id == chunk_id
    assert edge.chunk_ord == 0
    assert edge.chunk_text == _PASSAGE
    assert edge.sentence == _CLAIM
    assert edge.source_kind == "paper"
    assert edge.relation == "corroborates"


def test_withheld_cohort_skips_stamped_and_signed_off_edges(store: Any) -> None:
    _seed_edge(store, meta={"support": "yes"})
    _seed_edge(
        store,
        claim=_OTHER_CLAIM,
        passage=_OTHER_CLAIM,
        meta={"publish_signoff": {"by": "reto", "note": "checked by hand"}},
    )

    assert select_withheld_edges(store) == []


def test_withheld_cohort_requires_a_strict_claim_hub(store: Any) -> None:
    # A bare finding (no TAPROOT:claim / STATUS:canonical tags -- e.g. a
    # chase-tree finding) is not a hub; its edges are not this sweep's.
    paper = seed_ref(store, title="Lee 2008", kind="paper")
    seed_chunk(store, ref_id=paper, text=_PASSAGE, ord=0)
    finding = seed_ref(store, title=_CLAIM, kind="finding")
    store.add_link(
        src_ref_id=paper,
        dst_ref_id=finding,
        relation="corroborates",
        src_pos=0,
        meta={},
    )

    assert select_withheld_edges(store) == []


def test_passageless_edge_is_skipped_and_counted(store: Any) -> None:
    _seed_edge(store, pinned=False)

    assert select_withheld_edges(store) == []
    assert count_passageless_edges(store) == 1
    # Not the other cohort's problem: the stamp predicate differs.
    assert count_passageless_edges(store, unverified_stamped=True) == 0


def test_hub_filter_and_limit_are_respected(store: Any) -> None:
    hub_a, _pa, _ca, link_a = _seed_edge(store)
    hub_b, _pb, _cb, link_b = _seed_edge(
        store, claim=_OTHER_CLAIM, passage=_OTHER_CLAIM
    )

    assert [e.link_id for e in select_withheld_edges(store)] == [link_a, link_b]
    scoped = select_withheld_edges(store, hub_ref_id=hub_b)
    assert [e.link_id for e in scoped] == [link_b]
    assert [e.link_id for e in select_withheld_edges(store, limit=1)] == [link_a]


def test_unverified_stamped_cohort_selects_only_untrustworthy_stamps(
    store: Any,
) -> None:
    _h, _p, _c, born = _seed_edge(store, meta=dict(_BORN_RELEASED_META))
    # Withheld (no stamp at all) -- the DEFAULT cohort's row, not this one's.
    _seed_edge(store, claim=_OTHER_CLAIM, passage=_OTHER_CLAIM)
    # A settled verdict: verified AND sha-stamped, so the sentence it judged
    # is known. Nothing to re-verify.
    _seed_edge(
        store,
        claim=_THIRD_CLAIM,
        passage=_THIRD_CLAIM,
        meta={
            "support": "yes",
            "support_reason": "verified elsewhere",
            "verified_by": "verify-edges",
            "verified_claim_sha": "0" * 64,
        },
    )

    assert [e.link_id for e in select_unverified_stamped_edges(store)] == [born]


def test_unverified_stamped_cohort_reaches_a_verified_edge_with_no_sha(
    store: Any,
) -> None:
    """Regression: a verdict written before ``verified_claim_sha`` existed.

    It has a ``verified_by``, so this cohort used to exclude it; it has a
    ``support``, so the withheld cohort excludes it too. That left 311 prod
    edges over 186 live hubs (2026-08-31) permanently withheld by
    ``preflight.withheld_edges`` -- a NULL sha never equals the live
    title's -- with no CLI path to re-stamp them, and a ``--hub`` run
    reporting a bare ``0 edge(s) processed``.
    """
    _h, _p, _c, sha_less = _seed_edge(
        store,
        meta={
            "support": "partial",
            "support_reason": "judged against an unknown earlier sentence",
            "verified_by": "opus-5/retro-verify",
        },
    )

    assert [e.link_id for e in select_unverified_stamped_edges(store)] == [sha_less]
    # And it must NOT leak into the default cohort: it carries a support
    # value, so the strip-on-non-corroboration write is the right treatment.
    assert [e.link_id for e in select_withheld_edges(store)] == []
    assert sha_less is not None


# ── verify_edge -- the write path ────────────────────────────────────────


def test_dry_run_stamps_nothing(store: Any, monkeypatch: Any) -> None:
    _hub, _paper, _chunk, link_id = _seed_edge(store)
    _patch_verify(monkeypatch, _VERDICT_YES)
    before = _link_meta(store, link_id)

    (edge,) = select_withheld_edges(store)
    result = verify_edge(store, edge)  # apply=False is the default

    # The verdict is complete -- it just isn't written.
    assert result.status == "verified"
    assert result.applied is False
    assert result.action == "would-stamp"
    assert result.supports == "yes"
    assert _link_meta(store, link_id) == before


def test_apply_stamps_a_corroborating_verdict_with_the_full_meta(
    store: Any, monkeypatch: Any
) -> None:
    """The whole point: all six keys land on the SAME link_id via a jsonb
    MERGE (unrelated keys survive), and no second edge appears."""
    _hub, _paper, chunk_id, link_id = _seed_edge(store)
    _patch_verify(monkeypatch, _VERDICT_PARTIAL)
    before_count = _link_count(store)

    (edge,) = select_withheld_edges(store)
    result = verify_edge(store, edge, apply=True)

    assert result.status == "verified"
    assert result.applied is True
    assert result.action == "stamped"

    meta = _link_meta(store, link_id)
    assert meta["support"] == "partial"
    assert meta["support_reason"] == "supports under a narrower regime"
    assert meta["caveats"] == ["only tested at room temperature"]
    assert meta["verified_by"] == "verify-edges"
    assert meta["verified_claim_sha"] == claim_sha(_CLAIM)
    # UTC ISO timestamp -- parseable and timezone-aware.
    assert datetime.fromisoformat(meta["verified_at"]).tzinfo is not None
    # jsonb MERGE preserves unrelated keys; the row is UPDATEd, never
    # re-attached (attach_evidence would have inserted a second edge).
    assert meta["source_handle"] == f"pc{chunk_id}"
    assert _link_count(store) == before_count
    # Stamped => out of BOTH cohorts (support set + verified_by present).
    assert select_withheld_edges(store) == []
    assert select_unverified_stamped_edges(store) == []


def test_non_corroborating_default_cohort_reports_and_writes_nothing(
    store: Any, monkeypatch: Any
) -> None:
    """A no is reported for reground/human follow-up -- never stamped, even
    under --apply (pruning is reground's door)."""
    _hub, _paper, _chunk, link_id = _seed_edge(store)
    _patch_verify(monkeypatch, _VERDICT_NO)
    before = _link_meta(store, link_id)

    (edge,) = select_withheld_edges(store)
    result = verify_edge(store, edge, apply=True)

    assert result.status == "not-corroborated"
    assert result.applied is False
    assert result.action == "reported"
    assert result.supports == "no"
    assert _link_meta(store, link_id) == before
    assert "support" not in _link_meta(store, link_id)


def test_contradicting_partial_is_not_corroborating(
    store: Any, monkeypatch: Any
) -> None:
    # The real is_corroborating gate is in the loop: a partial flagged
    # contradicts is a non-corroboration, not a scoped support.
    _hub, _paper, _chunk, link_id = _seed_edge(store)
    _patch_verify(monkeypatch, {**_VERDICT_PARTIAL, "contradicts": True})

    (edge,) = select_withheld_edges(store)
    result = verify_edge(store, edge, apply=True)

    assert result.status == "not-corroborated"
    assert result.contradicts is True
    assert "support" not in _link_meta(store, link_id)


def test_llm_failure_is_a_skip_never_a_judgment(store: Any, monkeypatch: Any) -> None:
    _hub, _paper, _chunk, link_id = _seed_edge(store)
    _patch_verify(monkeypatch, None)
    before = _link_meta(store, link_id)

    (edge,) = select_withheld_edges(store)
    result = verify_edge(store, edge, apply=True)

    assert result.status == "llm-failed"
    assert result.applied is False
    assert result.action == "skipped"
    assert _link_meta(store, link_id) == before


def test_a_retired_pinned_chunk_is_skipped_not_verified(
    store: Any, monkeypatch: Any
) -> None:
    # The chunks join is a LEFT join on purpose: an edge pinned to a dead
    # row surfaces as chunk-missing instead of dropping out silently.
    _hub, _paper, chunk_id, _link_id = _seed_edge(store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s", (chunk_id,)
        )
        conn.commit()
    _never_verify(monkeypatch)

    (edge,) = select_withheld_edges(store)
    assert edge.chunk_text is None
    result = verify_edge(store, edge, apply=True)

    assert result.status == "chunk-missing"
    assert result.applied is False


def test_verify_receives_the_hub_sentence_passage_and_cite_info(
    store: Any, monkeypatch: Any
) -> None:
    calls: list[dict[str, Any]] = []
    _hub, paper, _chunk, _link = _seed_edge(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES ('cite_key', 'lee2008', %s, 'test')",
            (paper,),
        )
        conn.commit()
    _patch_verify(monkeypatch, _VERDICT_YES, calls=calls)

    (edge,) = select_withheld_edges(store)
    verify_edge(store, edge)

    assert calls == [
        {
            "claim": _CLAIM,
            "scope": {},
            "cite_key": "lee2008",
            "ord": 0,
            "chunk_text": _PASSAGE,
            "source_kind": "paper",
        }
    ]


# ── the --unverified-stamped cohort's writes ─────────────────────────────


def test_unverified_stamped_apply_overwrites_the_stamp_on_corroborate(
    store: Any, monkeypatch: Any
) -> None:
    _hub, _paper, chunk_id, link_id = _seed_edge(store, meta=dict(_BORN_RELEASED_META))
    _patch_verify(monkeypatch, _VERDICT_PARTIAL)

    (edge,) = select_unverified_stamped_edges(store)
    result = verify_edge(store, edge, apply=True, unverified_stamped=True)

    assert result.status == "verified"
    assert result.applied is True
    meta = _link_meta(store, link_id)
    # The mint-time default "yes" is overwritten with the real verdict.
    assert meta["support"] == "partial"
    assert meta["support_reason"] == "supports under a narrower regime"
    assert meta["caveats"] == ["only tested at room temperature"]
    assert meta["verified_by"] == "verify-edges"
    assert meta["verified_claim_sha"] == claim_sha(_CLAIM)
    assert meta["source_handle"] == f"pc{chunk_id}"
    # Re-verified => out of the cohort; re-running is a no-op.
    assert select_unverified_stamped_edges(store) == []


def test_unverified_stamped_apply_strips_support_on_non_corroborate(
    store: Any, monkeypatch: Any
) -> None:
    _hub, _paper, chunk_id, link_id = _seed_edge(store, meta=dict(_BORN_RELEASED_META))
    _patch_verify(monkeypatch, _VERDICT_NO)

    (edge,) = select_unverified_stamped_edges(store)
    result = verify_edge(store, edge, apply=True, unverified_stamped=True)

    assert result.status == "stripped"
    assert result.applied is True
    assert result.action == "stripped"
    meta = _link_meta(store, link_id)
    assert "support" not in meta  # back behind the publish gate
    assert "verified_by" not in meta  # a no/contradicts is never stamped
    assert meta["source_handle"] == f"pc{chunk_id}"  # unrelated keys survive
    # The stripped edge lands back in the DEFAULT withheld cohort.
    assert [e.link_id for e in select_withheld_edges(store)] == [link_id]


def test_unverified_stamped_dry_run_strips_nothing(
    store: Any, monkeypatch: Any
) -> None:
    _hub, _paper, _chunk, link_id = _seed_edge(store, meta=dict(_BORN_RELEASED_META))
    _patch_verify(monkeypatch, _VERDICT_NO)
    before = _link_meta(store, link_id)

    (edge,) = select_unverified_stamped_edges(store)
    result = verify_edge(store, edge, unverified_stamped=True)

    assert result.status == "stripped"
    assert result.applied is False
    assert result.action == "would-strip"
    assert _link_meta(store, link_id) == before


# ── `precis taproot verify-edges` ────────────────────────────────────────


def _cli_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "taproot_cmd": "verify-edges",
        "dry_run": False,
        "apply": False,
        "unverified_stamped": False,
        "hub": None,
        "limit": None,
        "out": None,
        "database_url": _active_dsn(),
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_dry_run_is_the_default_and_writes_nothing(
    store: Any, monkeypatch: Any, tmp_path: Any, capsys: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _hub, _paper, chunk_id, link_id = _seed_edge(store)
    # A passage-less sibling: counted in the summary, never verified.
    _seed_edge(store, claim=_OTHER_CLAIM, passage=_OTHER_CLAIM, pinned=False)
    before = _link_meta(store, link_id)
    _patch_verify(monkeypatch, _VERDICT_YES)
    out = tmp_path / "proposal.jsonl"

    taproot_cli.run(_cli_args(out=str(out)))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["link_id"] == link_id
    assert rows[0]["chunk_id"] == chunk_id
    assert rows[0]["supports"] == "yes"
    assert rows[0]["status"] == "verified"
    assert rows[0]["action"] == "would-stamp"
    assert rows[0]["applied"] is False
    err = capsys.readouterr().err
    assert "DRY-RUN" in err
    assert "skipped_passageless=1" in err
    # Nothing written -- the edge is still withheld.
    assert _link_meta(store, link_id) == before


def test_cli_apply_stamps_the_original_row(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _hub, _paper, _chunk, link_id = _seed_edge(store)
    before_count = _link_count(store)
    _patch_verify(monkeypatch, _VERDICT_YES)
    out = tmp_path / "applied.jsonl"

    taproot_cli.run(_cli_args(apply=True, out=str(out)))

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["applied"] is True
    assert rows[0]["action"] == "stamped"
    meta = _link_meta(store, link_id)
    assert meta["support"] == "yes"
    assert meta["verified_by"] == "verify-edges"
    assert meta["verified_claim_sha"] == claim_sha(_CLAIM)
    assert _link_count(store) == before_count


def test_cli_hub_filter_accepts_a_fi_handle_and_limit_caps(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _hub_a, _pa, _ca, link_a = _seed_edge(store)
    hub_b, _pb, _cb, link_b = _seed_edge(
        store, claim=_OTHER_CLAIM, passage=_OTHER_CLAIM
    )
    _patch_verify(monkeypatch, _VERDICT_YES)

    out_hub = tmp_path / "hub.jsonl"
    taproot_cli.run(_cli_args(out=str(out_hub), hub=f"fi{hub_b}"))
    rows = [
        json.loads(line)
        for line in out_hub.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["link_id"] for r in rows] == [link_b]

    out_limit = tmp_path / "limit.jsonl"
    taproot_cli.run(_cli_args(out=str(out_limit), limit=1))
    rows = [
        json.loads(line)
        for line in out_limit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["link_id"] for r in rows] == [link_a]


def test_cli_unverified_stamped_apply_overwrites_and_strips(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _h1, _p1, _c1, holds = _seed_edge(store, meta=dict(_BORN_RELEASED_META))
    _h2, _p2, _c2, folds = _seed_edge(
        store,
        claim=_OTHER_CLAIM,
        passage="An unrelated discussion of municipal water policy.",
        meta=dict(_BORN_RELEASED_META),
    )
    # A withheld edge must NOT be selected in this mode.
    _h3, _p3, _c3, withheld = _seed_edge(
        store, claim=_THIRD_CLAIM, passage=_THIRD_CLAIM
    )

    def _per_edge(
        *,
        claim: str,
        scope: dict[str, Any],
        target_cite_key: str,
        target_chunk_ord: int,
        target_chunk_text: str,
        source_kind: str = "paper",
    ) -> dict[str, Any] | None:
        if "unrelated" in target_chunk_text:
            return dict(_VERDICT_NO)
        return dict(_VERDICT_YES)

    monkeypatch.setattr(verify_edges, "_verify_support_with_caveats", _per_edge)
    out = tmp_path / "unverified.jsonl"

    taproot_cli.run(_cli_args(apply=True, unverified_stamped=True, out=str(out)))

    rows = {
        r["link_id"]: r
        for r in (
            json.loads(line)
            for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    assert set(rows) == {holds, folds}
    assert rows[holds]["action"] == "stamped"
    assert rows[folds]["action"] == "stripped"

    holds_meta = _link_meta(store, holds)
    assert holds_meta["support"] == "yes"
    assert holds_meta["verified_by"] == "verify-edges"
    folds_meta = _link_meta(store, folds)
    assert "support" not in folds_meta
    assert "verified_by" not in folds_meta
    # The withheld edge was untouched by this mode.
    assert "support" not in _link_meta(store, withheld)


def test_cli_reports_a_per_edge_failure_as_an_error_row_and_exits_nonzero(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    from precis.cli import taproot as taproot_cli

    _hub, _paper, _chunk, link_id = _seed_edge(store)

    def _dead(**_kwargs: Any) -> dict[str, Any] | None:
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(verify_edges, "_verify_support_with_caveats", _dead)
    out = tmp_path / "errors.jsonl"

    with pytest.raises(SystemExit) as exc:
        taproot_cli.run(_cli_args(apply=True, out=str(out)))
    assert exc.value.code == 1

    rows = [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["link_id"] == link_id
    assert rows[0]["status"] is None
    assert "dispatch exploded" in rows[0]["error"]
    assert "support" not in _link_meta(store, link_id)


def test_cli_bad_hub_handle_exits_nonzero(store: Any) -> None:
    # A paper is not a claim hub -- resolve_hub_ref_id refuses it.
    from precis.cli import taproot as taproot_cli
    from precis.utils import handle_registry

    paper = seed_ref(store, title="not a hub", kind="paper")
    with pytest.raises(SystemExit) as exc:
        taproot_cli.run(_cli_args(hub=handle_registry.format_handle("paper", paper)))
    assert exc.value.code == 1
