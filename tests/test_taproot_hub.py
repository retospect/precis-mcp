"""Taproot Phase-2 slice 2b — the hub write-path (`src/precis/taproot/hub.py`).

DB-backed (real `refs`/`chunks`/`ref_tags`/`links` via the `store` fixture);
no LLM — `CanonicalClaim`/`Placement` are constructed directly. Pins the
single write door: mint a `TAPROOT:claim` hub, attach typed evidence edges
(guarding role + target), and route every `place()` action.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim, Placement
from precis.taproot.hub import (
    PROPHETIC_EXAMPLE_CAVEAT,
    apply_placement,
    attach_evidence,
    link_claims,
    mint_hub,
    refine_claim_sentence,
)
from tests.workers._helpers import seed_chunk, seed_ref

_PUB_ID_RE = re.compile(r"^[a-z2-7]{6}$")

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)

#: A sharper wording of ``_CLAIM`` — a distinct claim (distinct pub_id/hub),
#: the kind an editor mints then links back to the original via ``refines``.
_SHARPER_CLAIM = CanonicalClaim(
    sentence=(
        "Pd/C catalyzes aryl-halide Suzuki coupling at 25 °C with K2CO3 in "
        "aqueous ethanol, >90% yield."
    ),
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


def _link_meta(store: Any, src: int, dst: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (src, dst),
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _pub_id(store: Any, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (ref_id,),
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


def test_mint_hub_creates_a_taproot_claim_finding(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)

    with store.pool.connection() as conn:
        kind = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()[0]
    assert kind == "finding"
    assert _ref_tag(store, hub, "TAPROOT") == "claim"
    assert _ref_tag(store, hub, "STATUS") == "canonical"
    assert _finding_body(store, hub) == _CLAIM.sentence


def test_mint_hub_surfaces_under_default_status_finding_search(store: Any) -> None:
    """A hub carries ``STATUS:canonical``, off the chase-status axis, so a
    ``tags=['TAPROOT:claim']`` search must find it *without* an explicit
    ``status=`` — the ``established`` chase default would otherwise hide
    every hub (the wart this fix closes)."""
    hub = mint_hub(store, _CLAIM)
    handler = FindingHandler(hub=Hub(store=store))

    out = handler.search(tags=["TAPROOT:claim"])
    assert str(hub) in out.body

    # An explicit status= still wins verbatim: the hub is `canonical`, not
    # `established`, so asking for `established` finds nothing.
    established_only = handler.search(tags=["TAPROOT:claim"], status="established")
    assert str(hub) not in established_only.body


def test_mint_hub_mints_a_citable_pub_id(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)

    pub_id = _pub_id(store, hub)
    assert pub_id is not None
    assert _PUB_ID_RE.match(pub_id)


def test_mint_hub_pub_id_is_deterministic_over_claim_content(store: Any) -> None:
    # The pub_id a freshly-minted hub gets matches the pure seed derivation
    # (make_taproot_hub_paper_id -> make_pub_id) for that same claim content
    # -- i.e. the seed is a function of claim.sentence/claim.scope alone,
    # not of ref_id or insertion order. (We don't mint a *second* hub for
    # the identical claim here: canonicalization is supposed to prevent
    # two hubs for one claim, and doing so would legitimately collide on
    # the ref_identifiers (id_kind, id_value) UNIQUE constraint -- that's
    # a real-dup guard, not something this determinism check exercises.)
    hub = mint_hub(store, _CLAIM)

    expected = make_pub_id(make_taproot_hub_paper_id(_CLAIM.sentence, _CLAIM.scope))
    assert _pub_id(store, hub) == expected

    # Same claim content via an equal-but-distinct CanonicalClaim instance
    # (fresh dict) reproduces the identical seed/pub_id.
    same_claim = CanonicalClaim(sentence=_CLAIM.sentence, scope=dict(_CLAIM.scope))
    again = make_pub_id(
        make_taproot_hub_paper_id(same_claim.sentence, same_claim.scope)
    )
    assert again == expected


def test_mint_hub_pub_id_resolves_back_to_the_hub(store: Any) -> None:
    # Mirrors resolve.py::_lookup_finding's query shape: pub_id -> ref_id.
    hub = mint_hub(store, _CLAIM)
    pub_id = _pub_id(store, hub)

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id FROM ref_identifiers ri "
            "JOIN refs r ON r.ref_id = ri.ref_id "
            "WHERE ri.id_kind = 'pub_id' AND ri.id_value = %s "
            "AND r.deleted_at IS NULL",
            (pub_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == hub


def _pub_id_ref(store: Any, pub_id: str) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = 'pub_id' AND id_value = %s",
            (pub_id,),
        ).fetchone()
    return int(row[0]) if row else None


def _pub_id_count(store: Any, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (ref_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _card_ords(store: Any, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND ord < 0", (ref_id,)
        ).fetchall()
    return sorted(int(r[0]) for r in rows)


def _ref_title(store: Any, ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row else ""


# ── refine_claim_sentence ────────────────────────────────────────────────


def test_refine_claim_sentence_updates_title_and_body(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    new_sentence = "Pd/C reliably catalyzes Suzuki coupling of aryl halides at RT."

    result = refine_claim_sentence(store, hub, new_sentence)

    assert result["hub_ref_id"] == hub
    assert result["old_title"] == _CLAIM.sentence[:200]
    assert result["new_title"] == new_sentence[:200]
    assert _ref_title(store, hub) == new_sentence[:200]
    assert _finding_body(store, hub) == new_sentence


def test_refine_claim_sentence_replaces_ord0_chunk_not_updates_in_place(
    store: Any,
) -> None:
    """The old ord=0 finding_body row is gone (a fresh chunk_id lands at
    the same ord=0 slot) -- DELETE+INSERT, never an in-place UPDATE."""
    hub = mint_hub(store, _CLAIM)
    with store.pool.connection() as conn:
        old_chunk_id = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (hub,)
        ).fetchone()[0]

    refine_claim_sentence(store, hub, "A reworded, sharper claim sentence.")

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND ord = 0", (hub,)
        ).fetchone()
    assert row is not None
    new_chunk_id, text = row
    assert new_chunk_id != old_chunk_id
    assert text == "A reworded, sharper claim sentence."
    # The old chunk_id is truly gone (not just superseded) -- DELETE, not append.
    with store.pool.connection() as conn:
        stale = conn.execute(
            "SELECT 1 FROM chunks WHERE chunk_id = %s", (old_chunk_id,)
        ).fetchone()
    assert stale is None


def test_refine_claim_sentence_drops_stale_card_variants(store: Any) -> None:
    """No pass re-derives a hub's card_combined off content changes (see
    the docstring) -- the retitle door deletes ord<0 rows so a stale card
    never keeps matching the OLD wording in canon.block's ANN index."""
    hub = mint_hub(store, _CLAIM)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, -1, 'card_combined', %s)",
            (hub, _CLAIM.sentence),
        )
        conn.commit()
    assert _card_ords(store, hub) == [-1]

    refine_claim_sentence(store, hub, "A different, reworded claim sentence.")

    assert _card_ords(store, hub) == []


def test_refine_claim_sentence_mints_new_pub_id_and_keeps_old_as_alias(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    old_pub_id = _pub_id(store, hub)
    assert old_pub_id is not None
    new_sentence = "An entirely different wording of the catalysis claim."

    result = refine_claim_sentence(store, hub, new_sentence)

    expected_new_pub_id = make_pub_id(
        make_taproot_hub_paper_id(new_sentence, _CLAIM.scope)
    )
    assert result["pub_id"] == expected_new_pub_id
    assert result["pub_id"] != old_pub_id
    assert result["pub_id_alias_kept"] is True

    # Both pub_ids still resolve to the same hub -- the old handle keeps
    # working for prose that already cited it.
    assert _pub_id_ref(store, old_pub_id) == hub
    assert _pub_id_ref(store, expected_new_pub_id) == hub
    assert _pub_id_count(store, hub) == 2


def test_refine_claim_sentence_identical_resulting_sentence_is_a_noop_on_identifiers(
    store: Any,
) -> None:
    """Rewording to text that hashes to the SAME pub_id (e.g. only
    whitespace/case differences normalize_text_for_hash absorbs, or a
    revert to the original wording) writes no new ref_identifiers row."""
    hub = mint_hub(store, _CLAIM)
    assert _pub_id_count(store, hub) == 1

    result = refine_claim_sentence(store, hub, _CLAIM.sentence)

    assert result["pub_id"] == _pub_id(store, hub)
    assert result["pub_id_alias_kept"] is False
    assert _pub_id_count(store, hub) == 1


def test_refine_claim_sentence_rejects_pub_id_collision_with_another_ref(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    other_sentence = "Some other, already-canonicalized claim sentence."
    other_hub = mint_hub(store, CanonicalClaim(sentence=other_sentence, scope={}))

    with pytest.raises(ValueError, match=f"ref_id={other_hub}"):
        refine_claim_sentence(store, hub, other_sentence, scope={})

    # Nothing was written on the collision -- rolled back atomically.
    assert _ref_title(store, hub) == _CLAIM.sentence[:200]
    assert _finding_body(store, hub) == _CLAIM.sentence


def test_refine_claim_sentence_rejects_non_hub(store: Any) -> None:
    plain = seed_ref(store, title="a plain finding", kind="finding")
    with pytest.raises(ValueError, match="not a TAPROOT:claim hub"):
        refine_claim_sentence(store, plain, "a new sentence")


def test_refine_claim_sentence_rejects_empty_sentence(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    with pytest.raises(ValueError, match="non-empty sentence"):
        refine_claim_sentence(store, hub, "   ")


def test_refine_claim_sentence_scope_override_replaces_meta_scope(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    new_scope = {"material": "Pd/C", "method": "Suzuki coupling", "regime": "80C"}

    result = refine_claim_sentence(store, hub, _CLAIM.sentence, scope=new_scope)

    ref = store.get_ref(kind="finding", id=hub)
    assert (ref.meta or {}).get("scope") == new_scope
    # Same sentence, different scope -> different pub_id (scope feeds the hash).
    expected = make_pub_id(make_taproot_hub_paper_id(_CLAIM.sentence, new_scope))
    assert result["pub_id"] == expected
    assert _pub_id_ref(store, expected) == hub


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

    # A finding NOT tagged TAPROOT:claim (an editorial review note).
    review = seed_ref(store, title="acronym unexpanded", kind="finding")
    with store.pool.connection() as conn:
        store.add_tag(
            review, Tag.closed("TAPROOT", "review"), set_by="agent", conn=conn
        )
        conn.commit()
    with pytest.raises(BadInput):
        attach_evidence(
            store, hub_ref_id=review, paper_ref_id=paper, role="establishes"
        )

    # A non-finding ref is never a hub.
    with pytest.raises(BadInput):
        attach_evidence(store, hub_ref_id=paper, paper_ref_id=paper, role="establishes")


# ── attach_evidence — deterministic prophetic-example caveat ───────────
# (patent-evidence-parity phase 4: PATENT_EXAMPLE:prophetic on the
# grounding chunk -> a fixed caveat appended in attach_evidence itself,
# mechanically, never via the verify LLM.)


def _tag_chunk(store: Any, ref_id: int, ord_: int, ns: str, value: str) -> None:
    with store.pool.connection() as conn:
        store.add_tag(
            ref_id, Tag.closed(ns, value), set_by="system", pos=ord_, conn=conn
        )
        conn.commit()


def test_attach_evidence_appends_prophetic_caveat_for_patent_source(
    store: Any,
) -> None:
    hub = mint_hub(store, _CLAIM)
    patent = seed_ref(store, title="EP1 prophetic probe", kind="patent")
    seed_chunk(store, ref_id=patent, text="the mixture is stirred", ord=0)
    _tag_chunk(store, patent, 0, "PATENT_EXAMPLE", "prophetic")

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=patent,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"ref{patent}~0"},
    )

    meta = _link_meta(store, patent, hub)
    assert meta["caveats"] == [PROPHETIC_EXAMPLE_CAVEAT]


def test_attach_evidence_no_caveat_for_worked_patent_chunk(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    patent = seed_ref(store, title="EP1 worked probe", kind="patent")
    seed_chunk(store, ref_id=patent, text="the mixture was stirred", ord=0)
    _tag_chunk(store, patent, 0, "PATENT_EXAMPLE", "worked")

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=patent,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"ref{patent}~0"},
    )

    assert _link_meta(store, patent, hub)["caveats"] == []


def test_attach_evidence_no_caveat_for_untagged_patent_chunk(store: Any) -> None:
    """The classifier is async — an as-yet-unclassified chunk gets NO
    caveat, same as ``worked``/``none``: silence is the default."""
    hub = mint_hub(store, _CLAIM)
    patent = seed_ref(store, title="EP1 untagged probe", kind="patent")
    seed_chunk(store, ref_id=patent, text="an unclassified description passage", ord=0)

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=patent,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"ref{patent}~0"},
    )

    assert _link_meta(store, patent, hub)["caveats"] == []


def test_attach_evidence_no_caveat_for_paper_source(store: Any) -> None:
    """A paper-source edge is untouched -- the caveat lookup is patent-only
    (a ``PATENT_EXAMPLE`` tag never even exists on a paper chunk)."""
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Collins 2006", kind="paper")
    seed_chunk(store, ref_id=paper, text="a paper body paragraph", ord=0)

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"support": "yes", "caveats": [], "source_handle": f"ref{paper}~0"},
    )

    assert _link_meta(store, paper, hub)["caveats"] == []


def test_attach_evidence_prophetic_caveat_not_duplicated(store: Any) -> None:
    """Re-verify (or a verdict that already independently named the same
    caveat) never doubles it up."""
    hub = mint_hub(store, _CLAIM)
    patent = seed_ref(store, title="EP1 dedup probe", kind="patent")
    seed_chunk(store, ref_id=patent, text="the catalyst may be prepared by", ord=0)
    _tag_chunk(store, patent, 0, "PATENT_EXAMPLE", "prophetic")

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=patent,
        role="corroborates",
        meta={
            "support": "yes",
            "caveats": [PROPHETIC_EXAMPLE_CAVEAT],
            "source_handle": f"ref{patent}~0",
        },
    )

    assert _link_meta(store, patent, hub)["caveats"] == [PROPHETIC_EXAMPLE_CAVEAT]


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
    assert _ref_tag(store, hub, "TAPROOT") == "claim"
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


# ── link_claims — the claim→claim advisory write door (migration 0100) ──


def test_link_claims_writes_a_refines_edge(store: Any) -> None:
    original = mint_hub(store, _CLAIM)
    sharper = mint_hub(store, _SHARPER_CLAIM)

    wrote = link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original)

    assert wrote is True
    # Directed sharper -> original; no auto-mirror.
    assert _edge(store, sharper, original) == "refines"
    assert _edge(store, original, sharper) is None


def test_link_claims_is_idempotent(store: Any) -> None:
    original = mint_hub(store, _CLAIM)
    sharper = mint_hub(store, _SHARPER_CLAIM)

    assert link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original) is True
    # Re-running the same authoring step writes nothing and reports it.
    assert link_claims(store, from_hub_ref_id=sharper, to_hub_ref_id=original) is False

    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'refines'",
            (sharper, original),
        ).fetchone()[0]
    assert n == 1


def test_link_claims_rejects_self_link(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    with pytest.raises(BadInput):
        link_claims(store, from_hub_ref_id=hub, to_hub_ref_id=hub)


def test_link_claims_rejects_unknown_relation(store: Any) -> None:
    original = mint_hub(store, _CLAIM)
    sharper = mint_hub(store, _SHARPER_CLAIM)
    with pytest.raises(BadInput):
        link_claims(
            store,
            from_hub_ref_id=sharper,
            to_hub_ref_id=original,
            relation="related-to",  # not a claim-link relation (v1: only refines)
        )


def test_link_claims_rejects_non_hub_endpoints(store: Any) -> None:
    hub = mint_hub(store, _CLAIM)
    paper = seed_ref(store, title="Not a hub", kind="paper")

    # `from` is not a claim hub.
    with pytest.raises(BadInput):
        link_claims(store, from_hub_ref_id=paper, to_hub_ref_id=hub)
    # `to` is not a claim hub.
    with pytest.raises(BadInput):
        link_claims(store, from_hub_ref_id=hub, to_hub_ref_id=paper)


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
