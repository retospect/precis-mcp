"""Taproot Phase-2 slice 2b — the hub write-path (`src/precis/taproot/hub.py`).

DB-backed (real `refs`/`chunks`/`ref_tags`/`links` via the `store` fixture);
no LLM — `CanonicalClaim`/`Placement` are constructed directly. Pins the
single write door: mint a `FROLE:claim` hub, attach typed evidence edges
(guarding role + target), and route every `place()` action.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim, Placement
from precis.taproot.hub import apply_placement, attach_evidence, mint_hub
from tests.workers._helpers import seed_ref

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _ref_tag(store: Any, ref_id: int, ns: str) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s",
            (ref_id, ns),
        ).fetchone()
    return row[0] if row else None


def _edge(store: Any, src: int, dst: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    return row[0] if row else None


def _finding_body(store: Any, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0 "
            "AND chunk_kind = 'finding_body'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


# ── mint_hub ────────────────────────────────────────────────────────────


def test_mint_hub_creates_a_frole_claim_finding(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)

    with store.pool.connection() as conn:
        kind = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()[0]
    assert kind == "finding"
    assert _ref_tag(store, hub, "FROLE") == "claim"
    assert _ref_tag(store, hub, "STATUS") == "tracing"
    assert _finding_body(store, hub) == _CLAIM.sentence


# ── attach_evidence ─────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["establishes", "corroborates", "contradicts"])
def test_attach_evidence_writes_paper_to_hub_edge(store: Any, role: str) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role=role,
        meta={"support": "yes", "char_offset": 142},
    )

    # Directed paper -> hub.
    assert _edge(store, paper, hub) == role
    assert _edge(store, hub, paper) is None


def test_attach_evidence_reads_back_inbound_on_the_hub(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=paper, role="establishes")

    inbound = store.links_for(hub, direction="in", relation="establishes")
    assert any(link.src_ref_id == paper for link in inbound)


def test_attach_evidence_rejects_unknown_role(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")
    with pytest.raises(BadInput):
        attach_evidence(store, hub_ref_id=hub, paper_ref_id=paper, role="supports")


def test_attach_evidence_rejects_non_claim_target(store: Any) -> None:
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    # A finding NOT tagged FROLE:claim (an editorial review note).
    review = seed_ref(store, title="acronym unexpanded", kind="finding")
    with store.pool.connection() as conn:
        store.add_tag(review, Tag.closed("FROLE", "review"), set_by="agent", conn=conn)
        conn.commit()
    with pytest.raises(BadInput):
        attach_evidence(
            store, hub_ref_id=review, paper_ref_id=paper, role="establishes"
        )

    # A non-finding ref is never a hub.
    with pytest.raises(BadInput):
        attach_evidence(store, hub_ref_id=paper, paper_ref_id=paper, role="establishes")


# ── apply_placement — routes every place() action ───────────────────────


def test_apply_placement_attach(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Corroborator 2010", kind="paper")

    out = apply_placement(
        store,
        _CLAIM,
        Placement(action="attach", hub_ref_id=hub),
        paper_ref_id=paper,
    )

    assert out == hub
    assert _edge(store, paper, hub) == "corroborates"  # default role


def test_apply_placement_new_mints_and_attaches(store: Any) -> None:
    paper = seed_ref(store, title="Originator 2001", kind="paper")

    hub = apply_placement(
        store,
        _CLAIM,
        Placement(action="new"),
        paper_ref_id=paper,
        role="establishes",
    )

    assert hub is not None
    assert _ref_tag(store, hub, "FROLE") == "claim"
    assert _edge(store, paper, hub) == "establishes"


def test_apply_placement_new_contradicts_links_the_hubs(store: Any) -> None:
    existing = mint_hub(
        store,
        CanonicalClaim(
            sentence="Pd/C does NOT catalyze Suzuki coupling at RT.", scope={}
        ),
    )
    paper = seed_ref(store, title="Contra 2015", kind="paper")

    hub = apply_placement(
        store,
        _CLAIM,
        Placement(action="new_contradicts", contradicts_hub_ref_id=existing),
        paper_ref_id=paper,
    )

    assert hub is not None
    assert hub != existing
    assert _edge(store, paper, hub) == "corroborates"  # paper supports the new claim
    assert _edge(store, hub, existing) == "contradicts"  # hub <-> hub opposition


def test_apply_placement_needs_review_files_todo_and_attaches_nothing(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Risky 2020", kind="paper")
    captured: list[tuple[CanonicalClaim, Placement]] = []

    placement = Placement(
        action="needs_review", hub_ref_id=hub, reason="low-confidence same"
    )
    out = apply_placement(
        store,
        _CLAIM,
        placement,
        paper_ref_id=paper,
        todo_fn=lambda claim, pl: captured.append((claim, pl)),
    )

    assert out is None
    assert captured == [(_CLAIM, placement)]
    assert _edge(store, paper, hub) is None  # nothing attached
