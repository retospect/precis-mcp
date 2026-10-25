"""Graph trust state, slice 2b (migration 0235): stored groundings,
the trust-subject sha, the admit gate and the structural apply path.

DB-backed (``store`` fixture). Owner identity under lax Q2: the
configured ``PRECIS_OWNER`` default ``"reto"`` (Config.owner;
the legacy ``owner`` placeholder is not an owner) — tests use ``reto``.
"""

from __future__ import annotations

from typing import Literal

import pytest

from precis.errors import BadInput, NotFound
from precis.graph.garden.apply import apply_structural
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import emit_proposal
from precis.store import Store
from precis.store.revision_context import TrustAct, revision_context
from precis.store.types import ChunkInsert
from precis.taproot import eligibility as el


def _ref(store: Store, kind: str = "concept", *, definition: str | None = None) -> int:
    meta = {"definition": definition} if definition is not None else {}
    with revision_context("t", trust_origin="generated", trust_actor_type="agent"):
        ref = store.insert_ref(kind=kind, slug=None, title=f"a {kind}", meta=meta)
    return int(ref.id)


def _body_chunk(store: Store, ref_id: int, text: str) -> int:
    with revision_context("t", trust_origin="generated", trust_actor_type="agent"):
        store.chunks.insert_chunks(
            ref_id,
            [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "memory_body"})],
        )
    row = store.pool.connection()
    with row as c:
        got = c.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0",
            (ref_id,),
        ).fetchone()
    assert got is not None
    return int(got[0])


def _consider(
    store: Store,
    kind: Literal["ref", "link"],
    target_id: int,
    actor: str = "agent",
) -> None:
    state = store.trust_state(kind, target_id)
    assert state is not None and state.sha
    store.record_trust_act(
        TrustAct(
            action="consider",
            target_kind=kind,
            target_id=target_id,
            expected_sha=state.sha,
            reason="read it",
        ),
        actor=actor,
        actor_type="agent",
    )


def _review(
    store: Store,
    ref_id: int,
    *,
    actor: str = "opus",
    model: str | None = "claude-opus-4-8",
    basis: Literal["content", "trust-subject"] = "trust-subject",
) -> int:
    r = store.record_target_review(
        "ref", ref_id, actor=actor, verdict="approved", model=model, sha_basis=basis
    )
    return int(r.review_id)


def _sourced(store: Store, ref_id: int, pin: str) -> int:
    g = store.record_grounding(
        "ref",
        ref_id,
        grounding="sourced",
        pins=[pin],
        actor="agent",
        actor_type="agent",
        reason="the use sits here",
    )
    return int(g.grounding_id)


def _make_admissible(store: Store, kind: str = "concept") -> tuple[int, str]:
    """A ref with definition + sourced use + current review + considered;
    returns (ref_id, pin handle)."""
    rid = _ref(store, kind, definition="a barrier for this test")
    _body_chunk(store, rid, "the term means this barrier")
    pin = f"fi{_ref(store, 'finding')}"
    _sourced(store, rid, pin)
    _review(store, rid)
    _consider(store, "ref", rid)
    return rid, pin


# ── storage: round-trip, history acts, no in-place upgrade ──────────────


def test_grounding_round_trip_and_history(store: Store) -> None:
    rid, pin = _make_admissible(store)
    gid = _sourced(store, rid, pin)
    rows = store.groundings_for("ref", rid)
    assert [g.grounding for g in rows] == ["sourced", "sourced"]
    assert store.grounding_set("ref", rid) == frozenset({"sourced"})
    acts = [a for a in store.trust_acts_for("ref", rid) if a["action"] == "ground"]
    assert len(acts) == 2 and acts[-1]["grounding"] == "sourced"
    assert acts[-1]["pins"][0]["handle"] == pin
    store.retire_grounding(gid, reason="superseded", actor="agent")
    acts = [a for a in store.trust_acts_for("ref", rid) if a["action"] == "unground"]
    assert len(acts) == 1 and acts[0]["grounding_id"] == gid
    assert store.grounding_set("ref", rid) == frozenset({"sourced"})


def test_sourced_requires_resolvable_pin(store: Store) -> None:
    rid = _ref(store, "memory")
    with pytest.raises(BadInput, match="grounding-pin-unresolvable"):
        store.record_grounding(
            "ref",
            rid,
            grounding="sourced",
            pins=["not-a-handle"],
            actor="a",
            actor_type="agent",
            reason="r",
        )
    with pytest.raises(BadInput, match="at least one pin"):
        store.record_grounding(
            "ref",
            rid,
            grounding="sourced",
            pins=[],
            actor="a",
            actor_type="agent",
            reason="r",
        )


def test_defined_needs_owner_review_and_considered(store: Store) -> None:
    rid = _ref(store)
    machine_review = _review(store, rid)  # opus model — a machine review
    _consider(store, "ref", rid)
    with pytest.raises(BadInput, match="defined-review-not-owner"):
        store.record_grounding(
            "ref",
            rid,
            grounding="defined",
            review_id=machine_review,
            actor="a",
            actor_type="agent",
            reason="r",
        )
    owner_review = store.record_target_review(
        "ref", rid, actor="reto", verdict="approved", sha_basis="trust-subject"
    )
    g = store.record_grounding(
        "ref",
        rid,
        grounding="defined",
        review_id=int(owner_review.review_id),
        actor="reto",
        actor_type="human",
        reason="r",
    )
    state = store.trust_state("ref", rid)
    assert state is not None and g.definition_sha == state.sha
    label = store.definition_label("ref", rid)
    assert label is not None and label.startswith("definition (reto,")
    # not-considered path
    rid2 = _ref(store)
    review2 = store.record_target_review(
        "ref", rid2, actor="reto", verdict="approved", sha_basis="trust-subject"
    )
    with pytest.raises(BadInput, match="defined-not-considered"):
        store.record_grounding(
            "ref",
            rid2,
            grounding="defined",
            review_id=int(review2.review_id),
            actor="reto",
            actor_type="human",
            reason="r",
        )


def test_sibling_grounding_no_inplace_upgrade(store: Store) -> None:
    rid = _ref(store)
    _consider(store, "ref", rid)
    owner_review = store.record_target_review(
        "ref", rid, actor="reto", verdict="approved", sha_basis="trust-subject"
    )
    store.record_grounding(
        "ref",
        rid,
        grounding="defined",
        review_id=int(owner_review.review_id),
        actor="reto",
        actor_type="human",
        reason="r",
    )
    _sourced(store, rid, f"fi{_ref(store, 'finding')}")
    assert store.grounding_set("ref", rid) == frozenset({"defined", "sourced"})
    defs = [g for g in store.groundings_for("ref", rid) if g.grounding == "defined"]
    assert len(defs) == 1 and defs[0].retired_at is None


# ── the admit gate: each refusal by name ────────────────────────────────


def test_admit_refuses_each_missing_condition(store: Store) -> None:
    # nothing present
    rid = _ref(store, "memory")
    with pytest.raises(BadInput, match="admit-no-definition"):
        store.admit_ref(rid, reason="r", actor="a")
    # definition only -> no sourced use
    rid = _ref(store, definition="x")
    _body_chunk(store, rid, "body")
    with pytest.raises(BadInput, match="admit-no-sourced-use"):
        store.admit_ref(rid, reason="r", actor="a")
    # + sourced -> no current review
    _sourced(store, rid, f"fi{_ref(store, 'finding')}")
    with pytest.raises(BadInput, match="admit-no-current-review"):
        store.admit_ref(rid, reason="r", actor="a")
    # + review -> not considered
    _review(store, rid)
    with pytest.raises(BadInput, match="admit-not-considered"):
        store.admit_ref(rid, reason="r", actor="a")


def test_admit_records_act_and_is_admitted(store: Store) -> None:
    rid, _pin = _make_admissible(store)
    out = store.admit_ref(rid, reason="admit this term", actor="agent")
    assert out["admitted"] and store.is_admitted(rid)
    act = out["act"]
    assert act["action"] == "admit" and act["target_kind"] == "ref"
    # replay with same op id semantics: a second admit records a second act
    out2 = store.admit_ref(rid, reason="admit this term", actor="agent")
    assert out2["act"]["op_id"] != act["op_id"]
    # demotion un-admits
    _demote(store, rid)
    assert not store.is_admitted(rid)


def _demote(store: Store, ref_id: int) -> None:
    state = store.trust_state("ref", ref_id)
    assert state is not None
    store.record_trust_act(
        TrustAct(
            action="demote",
            target_kind="ref",
            target_id=ref_id,
            expected_sha=state.sha,
            confidence="hunch",
            reason="untrust",
        ),
        actor="a",
        actor_type="agent",
    )


def test_admit_on_missing_ref(store: Store) -> None:
    with pytest.raises(NotFound):
        store.admit_ref(99999999, reason="r", actor="a")


# ── the trust-subject sha ───────────────────────────────────────────────


def test_comment_does_not_stale_consider_body_edit_does(store: Store) -> None:
    from tests._refs import insert_gripe

    with revision_context("t", trust_origin="generated", trust_actor_type="agent"):
        rid = int(insert_gripe(store, "a gripe").id)
    _body_chunk(store, rid, "the gripe body")
    state = store.trust_state("ref", rid)
    assert state is not None
    subject_sha = state.sha
    with revision_context("t", trust_origin="generated", trust_actor_type="agent"):
        store.chunks.insert_chunks(
            rid,
            [
                ChunkInsert(
                    ord=1, text="a comment", meta={"chunk_kind": "gripe_comment"}
                )
            ],
        )
    after = store.trust_state("ref", rid)
    assert after is not None and after.sha == subject_sha
    _consider(store, "ref", rid)  # the consider's expected_sha still matches
    with revision_context("t", trust_origin="generated", trust_actor_type="agent"):
        store.chunks.insert_chunks(
            rid,
            [ChunkInsert(ord=2, text="edit", meta={"chunk_kind": "memory_body"})],
        )
    after2 = store.trust_state("ref", rid)
    assert after2 is not None and after2.sha != subject_sha
    with pytest.raises(BadInput, match="stale"):
        store.record_trust_act(
            TrustAct(
                action="consider",
                target_kind="ref",
                target_id=rid,
                expected_sha=subject_sha,
                reason="stale",
            ),
            actor="a",
            actor_type="agent",
        )


def test_sha_basis_recorded(store: Store) -> None:
    rid = _ref(store)
    r = store.record_target_review(
        "ref",
        rid,
        actor="opus",
        verdict="approved",
        model="m",
        sha_basis="trust-subject",
    )
    assert r.sha_basis == "trust-subject"
    rows = store.reviews_for("ref", rid)
    assert rows[0].sha_basis == "trust-subject" and rows[0].current
    # a content-basis review goes stale on a comment; subject basis survives
    r2 = store.record_target_review(
        "ref",
        rid,
        actor="opus",
        verdict="approved",
        model="m",
        sha_basis="content",
    )
    with revision_context("t", trust_origin="generated", trust_actor_type="agent"):
        store.chunks.insert_chunks(
            rid,
            [ChunkInsert(ord=9, text="c", meta={"chunk_kind": "gripe_comment"})],
        )
    rows = {x.review_id: x for x in store.reviews_for("ref", rid)}
    assert not rows[r2.review_id].current and rows[r.review_id].current


def test_grounding_review_and_pin_staleness(store: Store) -> None:
    rid, pin = _make_admissible(store)
    gid = _sourced(store, rid, pin)
    r = store.record_target_review(
        "grounding",
        gid,
        actor="checker",
        verdict="approved",
        model="m",
        sha_basis="trust-subject",
    )
    assert r.current
    # retiring the pinned finding stales the grounding certificate
    with store.pool.connection() as c:
        c.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (int(pin[2:]),)
        )
    assert not store.reviews_for("grounding", gid)[0].current


# ── link-time refusal ───────────────────────────────────────────────────


def test_evidential_edge_refused_on_definition_only_endpoint(store: Store) -> None:
    src = _ref(store, "finding")
    dst = _ref(store)
    _consider(store, "ref", dst)
    owner_review = store.record_target_review(
        "ref", dst, actor="reto", verdict="approved", sha_basis="trust-subject"
    )
    store.record_grounding(
        "ref",
        dst,
        grounding="defined",
        review_id=int(owner_review.review_id),
        actor="reto",
        actor_type="human",
        reason="r",
    )
    with pytest.raises(BadInput, match="definition-only"):
        store.add_link(src_ref_id=src, dst_ref_id=dst, relation="establishes")
    # no edge persisted
    assert store.links_for(src, relation="establishes") == []
    # a sourced sibling on the endpoint lifts the refusal
    _sourced(store, dst, f"fi{_ref(store, 'finding')}")
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="establishes")
    assert store.links_for(src, relation="establishes") != []
    # structural edges are untouched either way
    dst2 = _ref(store)
    store.add_link(src_ref_id=src, dst_ref_id=dst2, relation="part-of")


# ── apply_structural: Opus and Reto paths ───────────────────────────────


def _structural_proposal(store: Store, child: int, parent: int) -> int:
    cand = GardenCandidate(
        detector="orphan",
        action="relink",
        target_kind="ref",
        target_id=child,
        other_id=parent,
        title="orphan taxon",
        payload={"parent": parent, "relation": "specialises"},
    )
    rid = emit_proposal(store, cand, rationale="no path to a start")
    assert rid is not None
    return int(rid)


def test_apply_structural_by_opus_not_defined(store: Store) -> None:
    child = _ref(store, "taxon")
    parent = _ref(store, "taxon")
    prop = _structural_proposal(store, child, parent)
    out = apply_structural(store, prop, actor="opus-review", model="claude-opus-4-8")
    assert out["relation"] == "specialises" and out["defined_grounding_id"] is None
    links = store.links_for(child, relation="specialises")
    assert len(links) == 1 and int(links[0].dst_ref_id) == parent
    assert store.grounding_set("link", out["link_id"]) == frozenset()
    # the machine review of the proposal is recorded
    revs = [r for r in store.reviews_for("ref", child) if r.verdict == "approved"]
    assert any(r.model == "claude-opus-4-8" for r in revs)


def test_apply_structural_by_owner_defined(store: Store) -> None:
    child = _ref(store, "taxon")
    parent = _ref(store, "taxon")
    prop = _structural_proposal(store, child, parent)
    out = apply_structural(store, prop, actor="reto")
    assert out["defined_grounding_id"] is not None
    assert store.grounding_set("link", out["link_id"]) == frozenset({"defined"})
    # edge is considered and origin stays generated
    st = store.trust_state("link", out["link_id"])
    assert st is not None
    assert (st.origin, st.confidence) == ("generated", "considered")


def test_apply_structural_stale_proposal_refused(store: Store) -> None:
    child = _ref(store, "taxon")
    parent = _ref(store, "taxon")
    prop = _structural_proposal(store, child, parent)
    _body_chunk(store, child, "drift the subject")
    with pytest.raises(BadInput, match="structural-proposal-stale"):
        apply_structural(store, prop, actor="reto")


def test_apply_structural_bare_actor_refused(store: Store) -> None:
    child = _ref(store, "taxon")
    parent = _ref(store, "taxon")
    prop = _structural_proposal(store, child, parent)
    with pytest.raises(BadInput, match="structural-apply-not-reviewed"):
        apply_structural(store, prop, actor="some-agent")


def test_eligibility_sees_stored_groundings(store: Store) -> None:
    rid, pin = _make_admissible(store)
    gs = el.stored_groundings(store, [("ref", rid)])
    assert gs[("ref", rid)] == {el.Grounding.SOURCED}
    assert el.is_definition_only({el.Grounding.DEFINED})
    assert not el.is_definition_only({el.Grounding.DEFINED, el.Grounding.SOURCED})
