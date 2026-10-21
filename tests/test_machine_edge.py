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
from precis.store.revision_context import revision_context, trust_producer
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
        a, b = _ref(store, "taxon", "a"), _ref(store, "taxon", "b")
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
