"""Machine-edge write rule — graph-gardener-value slice 1.

DB-backed (``store`` fixture). A *machine writer* is a write context whose
resolved ``trust_origin`` is ``generated``/``computed`` **and** which
carries a model identity (``precis.model``) — the future gardener/mesher/
scout doors announce themselves that way. Every refusal is a named
``BadInput``; an accepted edge round-trips its trust stamp plus the
``meta.{model,prompt_version,run_id,why}`` annotation. Human and
agent-without-model writes are untouched.

``MockEmbedder`` distances are binary (identical card text → ~0,
unrelated → ~1), which is what lets the co-retrieval refusal sit on both
sides of the ``+recall`` floor.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.graph.garden.apply import apply_merge
from precis.store import Store
from precis.store._links_ops import GARDEN_RELINK_META
from precis.store.revision_context import revision_context, trust_producer
from precis.store.types import Tag
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import link_claims, mint_hub

_EMB = MockEmbedder(dim=1024)
_EMBEDDER = "bge-m3"  # the migration-seeded default embedder


def _ref(
    store: Store, kind: str, title: str, meta: dict[str, Any] | None = None
) -> int:
    slug = f"me-{uuid.uuid4().hex[:10]}" if kind == "paper" else None
    return int(
        store.insert_ref(kind=kind, slug=slug, title=title, meta=dict(meta or {})).id
    )


def _card(store: Store, ref_id: int, text: str) -> None:
    """A ``card_combined`` chunk plus its default-embedder vector."""
    cid = store.chunks.upsert_card_combined(ref_id, text)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, "
            "attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _EMBEDDER, _EMB.embed_one(text)),
        )


def _claim_hub(store: Store, sentence: str) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def _machine():
    """The canonical machine-writer context: a converted producer origin
    plus the model identity behind the write."""
    return revision_context(
        "machine-edge test",
        model="test-model",
        trust_origin="generated",
        trust_actor_type="agent",
    )


_ANNOTATION: dict[str, Any] = {
    "model": "test-model",
    "prompt_version": "v1",
    "run_id": "run-1",
    "why": "the pair states the same barrier with opposite signs",
}
_EVIDENCE = ["pc1"]  # a chunk handle — the pin is shape-checked, not resolved


def _meta(**over: Any) -> dict[str, Any]:
    meta = {"evidence": list(_EVIDENCE), **_ANNOTATION}
    meta.update(over)
    return meta


def _link_row(store: Store, src: int, dst: int, relation: str) -> Any:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT link_id, trust_origin, trust_confidence, meta FROM links "
            "WHERE src_ref_id = %s AND dst_ref_id = %s AND relation = %s",
            (src, dst, relation),
        ).fetchone()


# ── who counts as a machine writer ─────────────────────────────────────


class TestMachineWriterDefinition:
    """How a write context becomes a machine writer — and which writes
    stay untouched."""

    def test_generated_plus_model_is_a_machine_writer(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with _machine():
            with pytest.raises(BadInput, match="machine-edge-human-only"):
                store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")

    def test_computed_plus_model_is_a_machine_writer(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with revision_context(
            "t", model="m", trust_origin="computed", trust_actor_type="worker"
        ):
            with pytest.raises(BadInput, match="machine-edge-human-only"):
                store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")

    def test_agent_without_model_is_untouched(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        # A converted writer's door alone (no model identity) — what the
        # link verb on a converted kind resolves to.
        with trust_producer("generated", actor_type="agent"):
            store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        assert _link_row(store, a, b, "related-to") is not None

    def test_human_write_of_related_to_still_works(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with revision_context("t", trust_origin="human", trust_actor_type="human"):
            store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        assert _link_row(store, a, b, "related-to") is not None

    def test_bare_write_is_untouched(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        assert _link_row(store, a, b, "related-to") is not None


# ── the refusals, by name ──────────────────────────────────────────────


class TestRefusals:
    def test_see_also_is_human_only(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with _machine(), pytest.raises(BadInput, match="machine-edge-human-only"):
            store.add_link(src_ref_id=a, dst_ref_id=b, relation="see-also")

    def test_structural_direct_write_is_refused(self, store: Store) -> None:
        a, b = (
            _ref(store, "taxon", "a"),
            _ref(store, "taxon", "b", meta={"start": "true"}),
        )
        with _machine():
            with pytest.raises(BadInput) as exc:
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="specialises",
                    meta=_meta(),
                )
            assert "machine-edge-structural" in exc.value.cause
            assert exc.value.next and "gardener-ledger proposal" in str(exc.value.next)

    def test_no_evidence_chunk_is_refused(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with _machine():
            with pytest.raises(BadInput, match="machine-edge-no-evidence"):
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="supports",
                    meta=dict(_ANNOTATION),
                )
            # a ref handle is not a chunk pin
            with pytest.raises(BadInput, match="machine-edge-no-evidence"):
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="supports",
                    meta=_meta(evidence=["pa1"]),
                )

    def test_missing_annotation_is_refused(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with _machine():
            with pytest.raises(BadInput, match="machine-edge-annotation") as exc:
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="supports",
                    meta={"evidence": ["pc1"]},
                )
            for key in ("model", "prompt_version", "run_id", "why"):
                assert key in exc.value.cause
            # why present but empty is still missing
            with pytest.raises(BadInput, match="machine-edge-annotation"):
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="supports",
                    meta=_meta(why="  "),
                )

    def test_co_retrievable_non_stance_edge_is_refused(self, store: Store) -> None:
        a, b = _ref(store, "paper", "a"), _ref(store, "paper", "b")
        _card(store, a, "same barrier text")
        _card(store, b, "same barrier text")  # identical → distance ~0
        with _machine():
            with pytest.raises(BadInput, match="machine-edge-co-retrievable"):
                store.add_link(
                    src_ref_id=a, dst_ref_id=b, relation="cites", meta=_meta()
                )

    def test_distant_non_stance_edge_is_accepted(self, store: Store) -> None:
        a, b = _ref(store, "paper", "a"), _ref(store, "paper", "b")
        _card(store, a, "catalysis barriers")
        _card(store, b, "retirement planning")
        with _machine():
            store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites", meta=_meta())
        assert _link_row(store, a, b, "cites") is not None

    def test_no_card_vector_means_no_co_retrieval_refusal(self, store: Store) -> None:
        a, b = _ref(store, "paper", "a"), _ref(store, "paper", "b")
        with _machine():
            store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites", meta=_meta())
        assert _link_row(store, a, b, "cites") is not None

    def test_stance_edge_skips_the_distance_check(self, store: Store) -> None:
        a, b = _ref(store, "paper", "a"), _ref(store, "paper", "b")
        _card(store, a, "same barrier text")
        _card(store, b, "same barrier text")
        with _machine():
            store.add_link(
                src_ref_id=a, dst_ref_id=b, relation="supports", meta=_meta()
            )
        assert _link_row(store, a, b, "supports") is not None


# ── the accepted edge ──────────────────────────────────────────────────


def test_accepted_stance_edge_round_trips_trust_and_why(store: Store) -> None:
    a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
    with _machine():
        link = store.add_link(
            src_ref_id=a, dst_ref_id=b, relation="supports", meta=_meta()
        )
    row = _link_row(store, a, b, "supports")
    assert row is not None and int(row[0]) == link.id
    # the 0214 trigger stamps origin/confidence from the write context
    assert (row[1], row[2]) == ("generated", "hunch")
    meta = row[3]
    for key in ("model", "prompt_version", "run_id", "why", "evidence"):
        assert meta[key] == _meta()[key]


# ── explains routing ───────────────────────────────────────────────────


class TestExplainsRouting:
    def test_hub_to_hub_routes_through_link_claims(self, store: Store) -> None:
        h1 = _claim_hub(store, "A mechanism lowers the barrier.")
        h2 = _claim_hub(store, "The barrier is lower under light.")
        with _machine():
            link = store.add_link(
                src_ref_id=h1, dst_ref_id=h2, relation="explains", meta=_meta()
            )
        row = _link_row(store, h1, h2, "explains")
        assert row is not None and int(row[0]) == link.id
        assert row[3]["why"] == _ANNOTATION["why"]

    def test_not_a_hub_pair_is_refused_to_the_door(self, store: Store) -> None:
        a, b = _ref(store, "memory", "a"), _ref(store, "memory", "b")
        with _machine():
            with pytest.raises(BadInput) as exc:
                store.add_link(
                    src_ref_id=a, dst_ref_id=b, relation="explains", meta=_meta()
                )
            assert "machine-edge-claim-hub" in exc.value.cause
            assert exc.value.next and "link_claims" in str(exc.value.next)

    def test_chunk_level_explains_is_refused(self, store: Store) -> None:
        h1 = _claim_hub(store, "A mechanism lowers the barrier.")
        h2 = _claim_hub(store, "The barrier is lower under light.")
        # both hubs, but explains is ref-level only — a chunk endpoint is
        # outside the hub→hub contract
        with _machine(), pytest.raises(BadInput, match="machine-edge-claim-hub"):
            store.add_link(
                src_ref_id=h1,
                dst_ref_id=h2,
                src_pos=0,
                relation="explains",
                meta=_meta(),
            )

    def test_hub_pair_still_needs_the_annotation(self, store: Store) -> None:
        h1 = _claim_hub(store, "A mechanism lowers the barrier.")
        h2 = _claim_hub(store, "The barrier is lower under light.")
        # explains is not an evidence edge (0203): no meta.evidence is
        # required — the hubs' own evidence is theirs — but the writer
        # annotation still applies.
        with _machine():
            with pytest.raises(BadInput, match="machine-edge-annotation"):
                store.add_link(
                    src_ref_id=h1,
                    dst_ref_id=h2,
                    relation="explains",
                    meta={"model": "m", "prompt_version": "v", "run_id": "r"},
                )

    def test_link_claims_direct_still_works(self, store: Store) -> None:
        h1 = _claim_hub(store, "A mechanism lowers the barrier.")
        h2 = _claim_hub(store, "The barrier is lower under light.")
        assert link_claims(
            store, from_hub_ref_id=h1, to_hub_ref_id=h2, relation="explains"
        )
        assert _link_row(store, h1, h2, "explains") is not None


# ── the link verb carries meta= to the gate (gr504789) ────────────────


class TestLinkVerbMeta:
    """``link(kind='finding'|'paper', ..., meta=...)`` through the real
    dispatch path. The verb refused ``meta=`` at the dispatch strictness
    gate, so a machine writer could not carry the annotation the
    admission gate requires — the hand-driven gardener pilot filed 12
    plain stance edges and parked the why/evidence ledger in a memory.
    ``meta`` is opt-in per kind: kinds that don't declare it (gripe
    here) still refuse it at the same gate."""

    def _finding(self, store: Store, title: str) -> int:
        return _ref(store, "finding", title)

    def test_finding_annotated_machine_edge_accepted(
        self, runtime_with_store, store: Store
    ) -> None:
        src = self._finding(store, "claim a")
        dst = self._finding(store, "claim b")
        with _machine():
            out = runtime_with_store.dispatch(
                "link",
                {
                    "kind": "finding",
                    "id": src,
                    "target": f"finding:{dst}",
                    "rel": "supports",
                    "meta": _meta(),
                },
            )
        assert "[error:" not in out, out
        row = _link_row(store, src, dst, "supports")
        assert row is not None
        meta = row[3]
        for key in ("model", "prompt_version", "run_id", "why", "evidence"):
            assert meta[key] == _meta()[key]

    def test_paper_annotated_machine_edge_accepted(
        self, runtime_with_store, store: Store
    ) -> None:
        slug = f"me-{uuid.uuid4().hex[:10]}"
        src = int(store.insert_ref(kind="paper", slug=slug, title="p", meta={}).id)
        dst = self._finding(store, "claim")
        with _machine():
            out = runtime_with_store.dispatch(
                "link",
                {
                    "kind": "paper",
                    "id": slug,
                    "target": f"finding:{dst}",
                    "rel": "supports",
                    "meta": _meta(),
                },
            )
        assert "[error:" not in out, out
        row = _link_row(store, src, dst, "supports")
        assert row is not None and row[3]["run_id"] == _ANNOTATION["run_id"]

    def test_machine_edge_via_verb_missing_pins_refused(
        self, runtime_with_store, store: Store
    ) -> None:
        src = self._finding(store, "claim a")
        dst = self._finding(store, "claim b")
        with _machine():
            out = runtime_with_store.dispatch(
                "link",
                {
                    "kind": "finding",
                    "id": src,
                    "target": f"finding:{dst}",
                    "rel": "supports",
                    "meta": dict(_ANNOTATION),
                },
            )
        assert "[error:BadInput]" in out
        assert "machine-edge-no-evidence" in out
        assert "next:" in out and "evidence" in out
        assert _link_row(store, src, dst, "supports") is None

    def test_machine_edge_via_verb_missing_annotation_refused(
        self, runtime_with_store, store: Store
    ) -> None:
        src = self._finding(store, "claim a")
        dst = self._finding(store, "claim b")
        with _machine():
            out = runtime_with_store.dispatch(
                "link",
                {
                    "kind": "finding",
                    "id": src,
                    "target": f"finding:{dst}",
                    "rel": "supports",
                    "meta": {"evidence": ["pc1"]},
                },
            )
        assert "[error:BadInput]" in out
        assert "machine-edge-annotation" in out
        assert "next:" in out
        assert _link_row(store, src, dst, "supports") is None

    def test_claim_hub_door_forwards_the_annotation(
        self, runtime_with_store, store: Store
    ) -> None:
        """The Taproot claim-link door (``link_claims``) also receives the
        caller's ``meta`` — the machine-edge gate inside ``add_link``
        sees the same annotation on a hub→hub ``refines`` edge."""
        h1 = _claim_hub(store, "Sharper wording of the claim.")
        h2 = _claim_hub(store, "The original claim.")
        with _machine():
            out = runtime_with_store.dispatch(
                "link",
                {
                    "kind": "finding",
                    "id": f"fi{h1}",
                    "target": f"fi{h2}",
                    "rel": "refines",
                    "meta": _meta(),
                },
            )
        assert "[error:" not in out, out
        row = _link_row(store, h1, h2, "refines")
        assert row is not None and row[3]["run_id"] == _ANNOTATION["run_id"]

    def test_evidence_door_merges_caller_meta_with_source_handle(
        self, runtime_with_store, store: Store
    ) -> None:
        """A machine edge through the paper→hub evidence door keeps the
        door's own ``source_handle`` meta AND the caller's annotation —
        the merge is what lets ``establishes`` pass the admission gate."""
        hub_id = _claim_hub(store, "Pd/C catalyzes Suzuki coupling.")
        paper = int(
            store.insert_ref(
                kind="paper",
                slug=f"me-{uuid.uuid4().hex[:10]}",
                title="p",
                meta={},
            ).id
        )
        from precis.store.types import ChunkInsert

        store.chunks.insert_chunks(paper, [ChunkInsert(ord=0, text="body", meta={})])
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
        with _machine():
            out = runtime_with_store.dispatch(
                "link",
                {
                    "kind": "finding",
                    "id": f"fi{hub_id}",
                    "target": f"pc{chunk_id}",
                    "rel": "establishes",
                    "meta": _meta(),
                },
            )
        assert "[error:" not in out, out
        row = _link_row(store, paper, hub_id, "establishes")
        assert row is not None
        meta = row[3]
        assert meta["run_id"] == _ANNOTATION["run_id"]
        assert meta["source_handle"] == f"pc{chunk_id}"

    def test_human_edge_meta_is_stored_not_gated(
        self, runtime_with_store, store: Store
    ) -> None:
        """A human/agent edge is untouched: ``meta=`` lands on the row
        with no annotation requirement, and a meta-less link still works
        exactly as before."""
        src = self._finding(store, "claim a")
        dst = self._finding(store, "claim b")
        out = runtime_with_store.dispatch(
            "link",
            {
                "kind": "finding",
                "id": src,
                "target": f"finding:{dst}",
                "rel": "related-to",
                "meta": {"note": "human annotation, no contract"},
            },
        )
        assert "[error:" not in out, out
        row = _link_row(store, src, dst, "related-to")
        assert row is not None and row[3]["note"] == "human annotation, no contract"

        src2 = self._finding(store, "claim c")
        out = runtime_with_store.dispatch(
            "link",
            {
                "kind": "finding",
                "id": src2,
                "target": f"finding:{dst}",
                "rel": "related-to",
            },
        )
        assert "[error:" not in out, out
        assert _link_row(store, src2, dst, "related-to") is not None

    def test_kind_without_meta_still_refuses_it(
        self, runtime_with_store, store: Store
    ) -> None:
        """Per-kind opt-in is pinned: ``gripe`` does not declare ``meta=``
        on its link, so the strictness gate refuses it exactly as it did
        for finding/paper before this change."""
        # gripe rows need exactly one STATUS tag at commit.
        ids = []
        for title in ("a", "b"):
            with store.tx() as conn:
                ref = store.insert_ref(
                    kind="gripe", slug=None, title=title, meta={}, conn=conn
                )
                store.add_tag(
                    ref.id, Tag.closed("STATUS", "open"), set_by="agent", conn=conn
                )
            ids.append(int(ref.id))
        a, b = ids
        out = runtime_with_store.dispatch(
            "link",
            {
                "kind": "gripe",
                "id": a,
                "target": f"gripe:{b}",
                "rel": "related-to",
                "meta": {"note": "x"},
            },
        )
        assert "[error:BadInput]" in out
        assert "does not accept" in out
        assert _link_row(store, a, b, "related-to") is None


# ── the merge exemption ────────────────────────────────────────────────


def test_merge_repointing_is_not_a_new_machine_edge(store: Store) -> None:
    winner = _ref(store, "paper", "winner")
    loser = _ref(store, "paper", "loser")
    other = _ref(store, "paper", "other")
    # a pre-existing human-only edge on the loser
    store.add_link(src_ref_id=other, dst_ref_id=loser, relation="related-to")
    with _machine():
        rec = apply_merge(store, loser, winner, actor="test")
    moved = _link_row(store, other, winner, "related-to")
    assert moved is not None
    assert moved[3]["garden_merge"] == rec.merge_id


# ── the reviewed structural door (gr507641) ──────────────────────────────


def _relink_proposal(store: Store, child: int, parent: int) -> int:
    """An orphan-detector-shaped pending relink proposal."""
    from precis.graph.garden.candidates import GardenCandidate
    from precis.graph.garden.proposals import emit_proposal

    rid = emit_proposal(
        store,
        GardenCandidate(
            detector="orphan",
            action="relink",
            target_kind="ref",
            target_id=child,
            other_id=parent,
            title="orphan relink",
            evidence={},
            payload={
                "child": child,
                "parent": parent,
                "relation": "specialises",
            },
        ),
        rationale="orphan taxon, nearest start node",
    )
    assert rid is not None
    return rid


def _relink_meta(review: int) -> dict[str, Any]:
    return {GARDEN_RELINK_META: {"relink_id": "garden-relink:t", "review": review}}


class TestReviewedStructuralDoor:
    def test_apply_relink_under_a_machine_writer_is_admitted(
        self, store: Store
    ) -> None:
        """The whole door under a machine context: the apply resolves
        the proposal, the gate admits the edge keyed on the approved
        review row, no evidence pins or annotation are needed — the
        review row is the record."""
        from precis.graph.garden.apply import apply_relink

        child = _ref(store, "taxon", "child")
        parent = _ref(store, "taxon", "parent", meta={"start": "true"})
        rid = _relink_proposal(store, child, parent)
        with _machine():
            rec = apply_relink(store, rid, actor="garden-consumer")
        row = _link_row(store, child, parent, "specialises")
        assert row is not None and int(row[0]) == rec.link_id
        stamp = row[3][GARDEN_RELINK_META]
        assert stamp["review"] == rec.review and stamp["proposal"] == rid
        assert row[1] == "generated"  # trust stamp from the write context

    def test_structural_write_without_a_review_still_refused(
        self, store: Store
    ) -> None:
        a, b = (
            _ref(store, "taxon", "a"),
            _ref(store, "taxon", "b", meta={"start": "true"}),
        )
        with _machine():
            for meta in (None, _meta()):  # annotated, but no review named
                with pytest.raises(BadInput, match="machine-edge-structural"):
                    store.add_link(
                        src_ref_id=a,
                        dst_ref_id=b,
                        relation="specialises",
                        meta=meta,
                    )
            # A caller-supplied garden_relink stamp outside the door is
            # refused outright — it would fake the undo record.
            with pytest.raises(BadInput, match="machine-edge-relink-stamp"):
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="specialises",
                    meta=_relink_meta(999999),
                )

    def test_stamp_meta_is_door_only_for_human_writes_too(self, store: Store) -> None:
        a, b = (
            _ref(store, "taxon", "a"),
            _ref(store, "taxon", "b", meta={"start": "true"}),
        )
        with pytest.raises(BadInput, match="machine-edge-relink-stamp"):
            store.add_link(
                src_ref_id=a,
                dst_ref_id=b,
                relation="specialises",
                meta=_relink_meta(999999),
            )

    def test_wrong_actor_approval_is_refused(self, store: Store) -> None:
        """A resolution written by a ledger identity (graph-garden)
        is not an approver — the stamp does not open the door."""
        from precis.graph.garden.proposals import resolve_proposal
        from precis.store._links_ops import relink_apply_door

        a, b = (
            _ref(store, "taxon", "a"),
            _ref(store, "taxon", "b", meta={"start": "true"}),
        )
        rid = _relink_proposal(store, a, b)
        resolved = resolve_proposal(
            store,
            rid,
            actor="graph-garden",
            verdict="approved",
            extra={"applied": "garden-relink:x:y:z"},
        )
        with (
            _machine(),
            relink_apply_door(),
            pytest.raises(BadInput, match="machine-edge-structural"),
        ):
            store.add_link(
                src_ref_id=a,
                dst_ref_id=b,
                relation="specialises",
                meta={
                    GARDEN_RELINK_META: {
                        "relink_id": "garden-relink:x:y:z",
                        "review": int(resolved.review_id),
                    }
                },
            )

    def test_crafted_approval_naming_a_pending_proposal_is_refused(
        self, store: Store
    ) -> None:
        """A non-terminal ``garden_review`` approve note is not a
        resolution — a stamp keyed on it is refused inside the door."""
        from precis.store._links_ops import relink_apply_door

        a, b = (
            _ref(store, "taxon", "a"),
            _ref(store, "taxon", "b", meta={"start": "true"}),
        )
        rid = _relink_proposal(store, a, b)
        crafted = store.record_target_review(
            "ref",
            a,
            actor="reto",
            verdict="approved",
            note=f'{{"garden_review": {{"proposal": {rid}, "verdict": "approve"}}}}',
        )
        with (
            _machine(),
            relink_apply_door(),
            pytest.raises(BadInput, match="machine-edge-structural"),
        ):
            store.add_link(
                src_ref_id=a,
                dst_ref_id=b,
                relation="specialises",
                meta={
                    GARDEN_RELINK_META: {
                        "relink_id": "garden-relink:x:y:z",
                        "review": int(crafted.review_id),
                    }
                },
            )

    def test_replayed_stamp_after_undo_is_refused(self, store: Store) -> None:
        """Single-use: after undo deletes the edge, the ``undone``
        tombstone keeps the same stamp from minting a new link."""
        from precis.graph.garden.apply import apply_relink, undo_relink
        from precis.store._links_ops import relink_apply_door

        a, b = (
            _ref(store, "taxon", "a"),
            _ref(store, "taxon", "b", meta={"start": "true"}),
        )
        rid = _relink_proposal(store, a, b)
        with _machine():
            rec = apply_relink(store, rid, actor="garden-consumer")
        undo_relink(store, rec.relink_id, actor="reto")
        with (
            _machine(),
            relink_apply_door(),
            pytest.raises(BadInput, match="machine-edge-structural"),
        ):
            store.add_link(
                src_ref_id=a,
                dst_ref_id=b,
                relation="specialises",
                meta={
                    GARDEN_RELINK_META: {
                        "relink_id": rec.relink_id,
                        "review": rec.review,
                    }
                },
            )
        # And while the edge is live, the same stamp cannot write twice —
        # re-apply is refused before the door: the proposal is decided.
        rid2 = _relink_proposal(
            store,
            _ref(store, "taxon", "c"),
            _ref(store, "taxon", "d", meta={"start": "true"}),
        )
        rec2 = apply_relink(store, rid2, actor="test")
        with (
            _machine(),
            relink_apply_door(),
            pytest.raises(BadInput, match="machine-edge-structural"),
        ):
            store.add_link(
                src_ref_id=rec2.child,
                dst_ref_id=rec2.parent,
                relation="specialises",
                meta={
                    GARDEN_RELINK_META: {
                        "relink_id": rec2.relink_id,
                        "review": rec2.review,
                    }
                },
            )


class TestApproverAllowlist:
    def test_human_approval_admits(self, store: Store) -> None:
        """A resolution written by a human (model NULL, non-worker
        actor) is an approver — the door admits the stamped edge."""
        from precis.graph.garden.apply import apply_relink

        child = _ref(store, "taxon", "child")
        parent = _ref(store, "taxon", "parent", meta={"start": "true"})
        rid = _relink_proposal(store, child, parent)
        with _machine():
            rec = apply_relink(store, rid, actor="reto")
        row = _link_row(store, child, parent, "specialises")
        assert row is not None and int(row[0]) == rec.link_id

    def test_worker_and_model_rows_do_not_approve(self, store: Store) -> None:
        """The allowlist is names, not just verdicts: a worker slug
        (``agent``) and a model-attributed row are not approvers even
        with a matching resolution payload."""
        from precis.graph.garden.proposals import resolve_proposal
        from precis.store._links_ops import relink_apply_door

        for actor, model in (("agent", None), ("reto", "opus-x")):
            a = _ref(store, "taxon", f"a-{actor}-{model}")
            b = _ref(store, "taxon", f"b-{actor}-{model}", meta={"start": "true"})
            rid = _relink_proposal(store, a, b)
            resolved = resolve_proposal(
                store,
                rid,
                actor=actor,
                verdict="approved",
                extra={"applied": "garden-relink:x:y:z"},
            )
            if model is not None:
                with store.pool.connection() as conn:
                    conn.execute(
                        "UPDATE reviews SET model = %s WHERE review_id = %s",
                        (model, int(resolved.review_id)),
                    )
            with (
                _machine(),
                relink_apply_door(),
                pytest.raises(BadInput, match="machine-edge-structural"),
            ):
                store.add_link(
                    src_ref_id=a,
                    dst_ref_id=b,
                    relation="specialises",
                    meta={
                        GARDEN_RELINK_META: {
                            "relink_id": "garden-relink:x:y:z",
                            "review": int(resolved.review_id),
                        }
                    },
                )
