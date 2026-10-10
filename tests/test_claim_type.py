"""Claim type — the persisted sort of a claim hub and the ``landscape``
policy (`src/precis/taproot/claim_type.py`; docs/backlog/
taproot-claim-model-v2.md, design 2026-10-10).

DB-backed via the ``store`` fixture; no LLM — ``CanonicalClaim`` carries
the type directly and the classify pass takes a fake ``classify_fn``.
Pins: the extractor field survives parsing; mint persists it and never
hashes it into identity; a landscape sentence converges every later mint
of the same sentence (the fi449493 → fi192855 fork); the write door's
human/llm precedence; a landscape hub never disputes.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.store.types import Tag
from precis.taproot import claim_type as ct
from precis.taproot.canon import CanonicalClaim, Placement, _parse_claim_item
from precis.taproot.hub import (
    apply_placement,
    link_claims,
    mint_hub,
    refine_claim_sentence,
)
from tests.workers._helpers import seed_ref

_LANDSCAPE = (
    "The electrical and optical properties of carbon nanomaterials are "
    "conventionally tuned by chemical or electrochemical doping."
)
_MEASURED = "Nanobud films show a work function of 4.9 eV by UPS."


def _meta(store: Any, ref_id: int) -> dict[str, Any]:
    return dict(store.fetch_refs_by_ids([ref_id])[ref_id].meta or {})


def _edge(store: Any, src: int, dst: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    return None if row is None else str(row[0])


# ── extractor field ───────────────────────────────────────────────────────


def test_parse_claim_item_keeps_a_known_type_and_drops_an_unknown_one() -> None:
    good = _parse_claim_item({"claim": "X is Y.", "type": " Landscape "})
    assert good is not None and good.claim_type == "landscape"
    bad = _parse_claim_item({"claim": "X is Y.", "type": "opinion"})
    assert bad is not None and bad.claim_type is None
    legacy = _parse_claim_item({"claim": "X is Y."})
    assert legacy is not None and legacy.claim_type is None


def test_policy_table_is_static_and_only_landscape_deviates() -> None:
    assert set(ct.POLICIES) == set(ct.CLAIM_TYPES)
    for t in ct.CLAIM_TYPES:
        if t != ct.LANDSCAPE:
            assert ct.policy_for(t) == ct.policy_for(None)
    land = ct.policy_for(ct.LANDSCAPE)
    assert not land.widen and not land.publishable and land.verifier == "consensus"
    assert ct.NO_WIDEN_TYPES == ("landscape",)
    assert "'landscape'" in ct.widenable_predicate_sql()


# ── mint ─────────────────────────────────────────────────────────────────


def test_mint_persists_the_type_as_llm_and_registers_the_sentence(store: Any) -> None:
    hub = mint_hub(
        store,
        CanonicalClaim(
            sentence=_LANDSCAPE,
            scope={"material": "carbon nanomaterials"},
            claim_type="landscape",
        ),
    )
    meta = _meta(store, hub)
    assert meta[ct.META_CLAIM_TYPE] == "landscape"
    assert meta[ct.META_CLAIM_TYPE_BY] == "llm"
    with store.pool.connection() as conn:
        assert ct.find_hub_by_sentence(conn, _LANDSCAPE) == hub


def test_untyped_mint_writes_no_type_keys_and_no_sentence_id(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=_MEASURED, scope={}))
    meta = _meta(store, hub)
    assert ct.META_CLAIM_TYPE not in meta and ct.META_CLAIM_TYPE_BY not in meta
    with store.pool.connection() as conn:
        assert ct.find_hub_by_sentence(conn, _MEASURED) is None


def test_landscape_sentence_converges_a_later_mint_under_a_leaked_scope(
    store: Any,
) -> None:
    """The fi449493 case: same sentence, the citing draft's subject leaked
    into scope. Scope is in the pub_id key, so without the sentence
    identity this forks a duplicate hub."""
    parent = mint_hub(
        store,
        CanonicalClaim(
            sentence=_LANDSCAPE,
            scope={"material": "carbon nanomaterials"},
            claim_type="landscape",
        ),
    )
    for leaked_type in ("landscape", "measurement", None):
        again = mint_hub(
            store,
            CanonicalClaim(
                sentence=_LANDSCAPE,
                scope={"material": "Carbon nanobuds"},
                claim_type=leaked_type,
            ),
        )
        assert again == parent


def test_non_landscape_hubs_still_fork_on_scope(store: Any) -> None:
    a = mint_hub(
        store,
        CanonicalClaim(
            sentence=_MEASURED, scope={"material": "A"}, claim_type="measurement"
        ),
    )
    b = mint_hub(
        store,
        CanonicalClaim(
            sentence=_MEASURED, scope={"material": "B"}, claim_type="measurement"
        ),
    )
    assert a != b


def test_type_is_not_part_of_the_identity_key(store: Any) -> None:
    a = mint_hub(
        store, CanonicalClaim(sentence=_MEASURED, scope={}, claim_type="mechanism")
    )
    b = mint_hub(
        store, CanonicalClaim(sentence=_MEASURED, scope={}, claim_type="capability")
    )
    assert a == b
    assert _meta(store, a)[ct.META_CLAIM_TYPE] == "mechanism"  # first writer wins


# ── the write door ────────────────────────────────────────────────────────


def test_set_claim_type_human_wins_llm_never_overwrites_or_clears(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=_MEASURED, scope={}))
    out = ct.set_claim_type(store, hub, "measurement", by="llm")
    assert out["claim_type"] == "measurement" and out["previous"] is None
    assert _meta(store, hub)[ct.META_CLAIM_TYPE_BY] == "llm"

    ct.set_claim_type(store, hub, "mechanism", by="human")
    assert _meta(store, hub)[ct.META_CLAIM_TYPE_BY] == "human"

    skipped = ct.set_claim_type(store, hub, "measurement", by="llm")
    assert skipped["skipped"] == "human"
    assert _meta(store, hub)[ct.META_CLAIM_TYPE] == "mechanism"

    with pytest.raises(BadInput):
        ct.set_claim_type(store, hub, None, by="llm")

    ct.set_claim_type(store, hub, None, by="human")
    meta = _meta(store, hub)
    assert (
        meta.get(ct.META_CLAIM_TYPE) is None and meta.get(ct.META_CLAIM_TYPE_BY) is None
    )


def test_set_claim_type_rejects_unknown_type_and_non_hub(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=_MEASURED, scope={}))
    with pytest.raises(BadInput, match="unknown claim_type"):
        ct.set_claim_type(store, hub, "opinion", by="human")
    paper = seed_ref(store, title="Not a hub", kind="paper")
    with pytest.raises(BadInput, match="not a live claim hub"):
        ct.set_claim_type(store, paper, "landscape", by="human")


def test_set_landscape_registers_sentence_pops_due_and_reports_duplicates(
    store: Any,
) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=_LANDSCAPE, scope={}))
    # A pre-existing fork: untyped mint, the draft's subject in scope, so
    # the pub_id differs and no sentence identity exists yet to converge on.
    twin = mint_hub(
        store, CanonicalClaim(sentence=_LANDSCAPE, scope={"material": "nanobuds"})
    )
    assert twin != hub
    store.add_tag(hub, Tag.closed("TAPROOT_DUE", "1"), set_by="system")

    out = ct.set_claim_type(store, hub, "landscape", by="human")
    assert out["popped_due"] is True and "duplicate_of" not in out
    with store.pool.connection() as conn:
        assert ct.find_hub_by_sentence(conn, _LANDSCAPE) == hub

    out = ct.set_claim_type(store, twin, "landscape", by="human")
    assert out["duplicate_of"] == hub  # merge candidate, never a silent merge


def test_refine_claim_sentence_re_registers_a_landscape_sentence(store: Any) -> None:
    hub = mint_hub(
        store, CanonicalClaim(sentence=_LANDSCAPE, scope={}, claim_type="landscape")
    )
    reworded = _LANDSCAPE.replace("conventionally", "routinely")
    refine_claim_sentence(store, hub, reworded)
    with store.pool.connection() as conn:
        assert ct.find_hub_by_sentence(conn, reworded) == hub
        assert ct.find_hub_by_sentence(conn, _LANDSCAPE) == hub  # alias kept


# ── disputes ──────────────────────────────────────────────────────────────


def test_a_landscape_hub_may_be_refined_but_never_disputes(store: Any) -> None:
    land = mint_hub(
        store, CanonicalClaim(sentence=_LANDSCAPE, scope={}, claim_type="landscape")
    )
    specific = mint_hub(
        store, CanonicalClaim(sentence=_MEASURED, scope={}, claim_type="measurement")
    )
    assert link_claims(store, from_hub_ref_id=specific, to_hub_ref_id=land) is True
    with pytest.raises(BadInput, match="cannot dispute"):
        link_claims(
            store, from_hub_ref_id=land, to_hub_ref_id=specific, relation="disputes"
        )
    # the specific claim may still dispute the landscape hub's wording
    assert (
        link_claims(
            store, from_hub_ref_id=specific, to_hub_ref_id=land, relation="disputes"
        )
        is True
    )


def test_placement_skips_the_hub_to_hub_disputes_when_a_side_is_landscape(
    store: Any,
) -> None:
    land = mint_hub(
        store, CanonicalClaim(sentence=_LANDSCAPE, scope={}, claim_type="landscape")
    )
    paper = seed_ref(store, title="Contra 2015", kind="paper")
    hub = apply_placement(
        store,
        CanonicalClaim(sentence=_MEASURED, scope={}, claim_type="measurement"),
        Placement(action="new_contradicts", contradicts_hub_ref_id=land),
        paper_ref_id=paper,
    )
    assert hub is not None and hub != land
    assert _edge(store, paper, hub) == "corroborates"
    assert _edge(store, hub, land) is None


# ── consensus + classify pass ────────────────────────────────────────────


def test_consensus_line_only_for_consensus_verified_types() -> None:
    assert ct.consensus_line("measurement", 10) is None
    assert "fail" in (ct.consensus_line("landscape", 2) or "")
    assert "pass" in (ct.consensus_line("landscape", 3) or "")


def test_run_classify_pass_is_idempotent_and_skips_humans(store: Any) -> None:
    a = mint_hub(store, CanonicalClaim(sentence=_MEASURED, scope={}))
    b = mint_hub(store, CanonicalClaim(sentence=_LANDSCAPE, scope={}))
    ct.set_claim_type(store, b, "definition", by="human")
    calls: list[str] = []

    def fake(sentence: str, scope: dict[str, Any]) -> str | None:
        calls.append(sentence)
        return "landscape" if sentence == _LANDSCAPE else "measurement"

    dry = ct.run_classify_pass(store, limit=50, apply=False, classify_fn=fake)
    assert [r["hub_ref_id"] for r in dry] == [a]  # b has a type already
    assert dry[0]["applied"] is False
    assert ct.META_CLAIM_TYPE not in _meta(store, a)

    wet = ct.run_classify_pass(store, limit=50, apply=True, classify_fn=fake)
    assert wet[0]["applied"] is True
    assert _meta(store, a)[ct.META_CLAIM_TYPE] == "measurement"
    assert ct.run_classify_pass(store, limit=50, apply=True, classify_fn=fake) == []
    assert _meta(store, b)[ct.META_CLAIM_TYPE] == "definition"


def test_run_classify_pass_with_workers_keeps_ref_id_order(store: Any) -> None:
    hubs = [
        mint_hub(store, CanonicalClaim(sentence=f"Hub {i} states fact {i}.", scope={}))
        for i in range(5)
    ]

    def fake(sentence: str, scope: dict[str, Any]) -> str | None:
        return "definition" if "fact 2" in sentence else "capability"

    rows = ct.run_classify_pass(
        store, limit=50, apply=True, classify_fn=fake, workers=4
    )
    assert [r["hub_ref_id"] for r in rows] == sorted(hubs)
    assert _meta(store, hubs[2])[ct.META_CLAIM_TYPE] == "definition"
    assert all(_meta(store, h)[ct.META_CLAIM_TYPE_BY] == "llm" for h in hubs)


def test_run_classify_pass_persists_each_batch_before_the_next(store: Any) -> None:
    """Kill-safety: by the time batch N's calls go out, batch N-1 is written
    and reported -- a run stopped mid-way keeps its finished batches."""
    hubs = sorted(
        mint_hub(
            store, CanonicalClaim(sentence=f"Batch hub {i} states fact {i}.", scope={})
        )
        for i in range(5)
    )
    seen_persisted: list[list[int]] = []
    batches: list[list[int]] = []

    def fake(sentence: str, scope: dict[str, Any]) -> str | None:
        seen_persisted.append(
            [h for h in hubs if _meta(store, h).get(ct.META_CLAIM_TYPE) is not None]
        )
        return "capability"

    rows = ct.run_classify_pass(
        store,
        limit=50,
        apply=True,
        classify_fn=fake,
        batch_size=2,
        on_batch=lambda done: batches.append([r["hub_ref_id"] for r in done]),
    )
    assert [r["hub_ref_id"] for r in rows] == hubs
    assert batches == [hubs[0:2], hubs[2:4], hubs[4:5]]
    # The third call (first of batch 2) already sees batch 1 persisted.
    assert seen_persisted[2] == hubs[0:2]
    assert seen_persisted[4] == hubs[0:4]
