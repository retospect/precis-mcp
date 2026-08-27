"""The hypothesis-proposal door — ``put(kind='finding', hypothesis=True, …)``.

DB-backed (real `refs`/`links`/`ref_tags` via the `store` fixture); no LLM.
Covers the two guards that stand in for the grounding invariant an ordinary
hub gets from `seed_claim_hub`, the motivation edges (which must never be
evidence edges), and the provenance links back to the note and the tick.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._finding_hypothesis import (
    ARTIFACT_HYPOTHESIS,
    META_ARTIFACT_TYPE,
    META_PROPOSED_PAYLOAD,
    PROPOSED_TAG,
)
from precis.handlers.finding import FindingHandler
from precis.store import Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import HUB_ROLES, attach_evidence, mint_hub
from tests.workers._helpers import seed_chunk, seed_ref

_SENTENCE = "Nanoindentation measures a modulus above 9 GPa in nanobud films."


def _handler(store: Store) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _paper_with_chunk(store: Store, title: str) -> tuple[int, int]:
    ref_id = seed_ref(store, title=title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=ref_id, text=f"A passage from {title}.")
    return ref_id, chunk_id


def _links(
    store: Store, hub_ref_id: int, relation: str
) -> list[tuple[int, int | None]]:
    with store.pool.connection() as conn:
        return [
            (int(r[0]), r[1])
            for r in conn.execute(
                "SELECT dst_ref_id, dst_chunk_id FROM links "
                "WHERE src_ref_id = %s AND relation = %s",
                (hub_ref_id, relation),
            ).fetchall()
        ]


def _hub_id(body: str) -> int:
    return int(body.split("fi", 1)[1].split()[0])


def test_proposal_mints_a_hypothesis_hub_with_motivation_edges(store: Store) -> None:
    """Two passages from two papers ⇒ a hub carrying the durable
    artifact-type marker, the prepared payload, the triage tag, and one
    chunk-granular motivation edge per motivator — and zero evidence."""
    pa1, ch1 = _paper_with_chunk(store, "Nanoindentation of nanobud films")
    pa2, ch2 = _paper_with_chunk(store, "Covalent fullerene-tube hybrids")

    resp = _handler(store).put(
        title=_SENTENCE,
        hypothesis=True,
        motivation="Both papers attribute stiffness to the covalent junction; "
        "the transfer to films is untested.",
        testable_by="nanoindentation of a pressed nanobud film versus pristine "
        "graphene under the same tip and load",
        motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        llm_models=["test-model", "test-model-verifier"],
    )

    hub_ref_id = _hub_id(resp.body)
    ref = store.fetch_refs_by_ids([hub_ref_id])[hub_ref_id]
    assert ref.title == _SENTENCE
    assert ref.meta[META_ARTIFACT_TYPE] == ARTIFACT_HYPOTHESIS

    payload = ref.meta[META_PROPOSED_PAYLOAD]
    assert payload["hypothesis"] is True
    # A hypothesis has no passage by definition, and with no passages any
    # structured field trips the gates' field-containment check.
    assert payload["passages"] == []
    assert payload["fields"] == {}
    assert payload["motivation"] and payload["testable_by"]
    # mint.py requires motivating refs to carry a SIGNED artifact, so the
    # handles ride as a hint for the reviewer to promote later.
    assert payload["motivated_by_refs"] == []
    assert payload["motivated_by_hint"] == [f"pc{ch1}", f"pc{ch2}"]
    # Parked verbatim: approve freezes this into `grounding`, sign folds it
    # into the pubinfo software node (precis:llmModel).
    assert payload["llm_models"] == ["test-model", "test-model-verifier"]

    motivated = dict(_links(store, hub_ref_id, "motivated-by"))
    assert set(motivated) == {pa1, pa2}
    # Chunk-granular: the edge remembers which passage provoked it.
    assert all(chunk_id is not None for chunk_id in motivated.values())

    # ...and none of it is evidence.
    for role in sorted(HUB_ROLES):
        assert _links(store, hub_ref_id, role) == []

    tags = {t.value for t in store.tags_for(hub_ref_id)}
    assert PROPOSED_TAG in tags


def test_ref_level_motivator_is_accepted_without_a_passage(store: Store) -> None:
    pa1, _ = _paper_with_chunk(store, "First source")
    pa2, _ = _paper_with_chunk(store, "Second source")

    resp = _handler(store).put(
        title=_SENTENCE,
        hypothesis=True,
        motivation="leap",
        testable_by="experiment",
        llm_models=["test-model"],
        motivated_by=[f"pa{pa1}", f"pa{pa2}"],
    )
    motivated = dict(_links(store, _hub_id(resp.body), "motivated-by"))
    assert set(motivated) == {pa1, pa2}
    assert all(chunk_id is None for chunk_id in motivated.values())


def test_one_motivator_is_refused(store: Store) -> None:
    pa1, ch1 = _paper_with_chunk(store, "Only source")
    with pytest.raises(BadInput, match="at least 2"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"pc{ch1}"],
        )
    assert pa1  # seeded, but nothing was minted


def test_two_hubs_on_one_paper_count_as_one_source(store: Store) -> None:
    """Two claim hubs grounded in the same single paper are a restatement,
    not the cross-binding the hypothesis type exists for."""
    pa1, ch1 = _paper_with_chunk(store, "Single shared source")
    hubs = []
    for sentence in ("DFT shows A rises by 12%.", "DFT shows B falls by 4%."):
        hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
        attach_evidence(
            store,
            hub_ref_id=hub,
            paper_ref_id=pa1,
            role="corroborates",
            meta={"source_handle": f"pc{ch1}"},
            check_retraction=False,
        )
        hubs.append(hub)

    with pytest.raises(BadInput, match="distinct source paper"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"fi{hubs[0]}", f"fi{hubs[1]}"],
        )


def test_a_hypothesis_cannot_stack_on_other_hypotheses(store: Store) -> None:
    """Conjecture built on conjecture is how a speculative pass talks itself
    into anything. A hypothesis hub has no evidence, so it contributes zero
    source papers and two of them can never clear the bar — the rule falls
    out of `_source_papers` rather than needing its own special case."""
    prior = [
        mint_hub(store, CanonicalClaim(sentence=s, scope={}))
        for s in (
            "DFT predicts an unmeasured shift in system A.",
            "DFT predicts an unmeasured shift in system B.",
        )
    ]
    with pytest.raises(BadInput, match="distinct source paper"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"fi{prior[0]}", f"fi{prior[1]}"],
        )


def test_two_structures_are_independent_sources(store: Store) -> None:
    """A quest campaign's own instrument measurements can motivate a
    hypothesis — two distinct `structure` refs clear the independence bar
    on their own."""
    st1 = seed_ref(store, title="Structure A", kind="structure")
    st2 = seed_ref(store, title="Structure B", kind="structure")

    resp = _handler(store).put(
        title=_SENTENCE,
        hypothesis=True,
        motivation="leap",
        testable_by="experiment",
        llm_models=["test-model"],
        motivated_by=[f"st{st1}", f"st{st2}"],
    )
    motivated = dict(_links(store, _hub_id(resp.body), "motivated-by"))
    assert set(motivated) == {st1, st2}


def test_a_structure_and_a_paper_together_are_independent_sources(
    store: Store,
) -> None:
    """Mixed motivators count fine — one measured structure plus one source
    paper is two independent sources."""
    st1 = seed_ref(store, title="Structure A", kind="structure")
    pa1, ch1 = _paper_with_chunk(store, "A real source")

    resp = _handler(store).put(
        title=_SENTENCE,
        hypothesis=True,
        motivation="leap",
        testable_by="experiment",
        llm_models=["test-model"],
        motivated_by=[f"st{st1}", f"pc{ch1}"],
    )
    motivated = dict(_links(store, _hub_id(resp.body), "motivated-by"))
    assert set(motivated) == {st1, pa1}


def test_one_structure_alone_fails_independence(store: Store) -> None:
    """The same single measured structure named twice is still one source —
    a structure gets no exemption from the restatement rule a repeated
    paper already faces."""
    st1 = seed_ref(store, title="Structure A", kind="structure")

    with pytest.raises(BadInput, match="distinct source paper"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"st{st1}", f"st{st1}"],
        )


def test_a_quest_is_not_a_citable_motivator(store: Store) -> None:
    """A quest is a container, not an observation — it stays disallowed
    even though a structure (also non-paper) is now accepted. The refused
    kind's `options` names structure among what *is* allowed."""
    st1 = seed_ref(store, title="Structure A", kind="structure")
    quest = seed_ref(store, title="a campaign", kind="quest")
    with pytest.raises(BadInput, match="'quest' ref") as exc:
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"st{st1}", f"qu{quest}"],
        )
    assert "structure" in (exc.value.options or [])


def test_a_memory_is_not_a_citable_motivator(store: Store) -> None:
    """A dream may think with its own notes; a signed artifact cites
    sources, and a note is not one."""
    pa1, ch1 = _paper_with_chunk(store, "A real source")
    mem = seed_ref(store, title="a prior dream", kind="memory")
    with pytest.raises(BadInput, match="memory"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"pc{ch1}", f"me{mem}"],
        )


def test_missing_testable_by_is_refused(store: Store) -> None:
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    with pytest.raises(BadInput, match="testable_by"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        )


def test_missing_llm_models_is_refused_before_anything_is_minted(
    store: Store,
) -> None:
    """The proposing agent is the only party who knows what model authored
    the conjecture — a human at the approve form cannot recover it later,
    so the door refuses up front (fi211520 shipped unattributed this way)."""
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    before = store.count_refs(kind="finding")

    for bad in (None, [], ["  "]):
        with pytest.raises(BadInput, match="llm_models"):
            _handler(store).put(
                title=_SENTENCE,
                hypothesis=True,
                motivation="leap",
                testable_by="experiment",
                motivated_by=[f"pc{ch1}", f"pc{ch2}"],
                llm_models=bad,
            )

    assert store.count_refs(kind="finding") == before


def test_mode_conflict_with_supporters(store: Store) -> None:
    _, ch1 = _paper_with_chunk(store, "First source")
    with pytest.raises(BadInput, match="exclusive"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"pc{ch1}"],
            supporters=[{"paper": "pa1"}],
        )


def test_origin_memory_is_linked(store: Store) -> None:
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    mem = seed_ref(store, title="the note that reasoned its way here", kind="memory")

    resp = _handler(store).put(
        title=_SENTENCE,
        hypothesis=True,
        motivation="leap",
        testable_by="experiment",
        llm_models=["test-model"],
        motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        from_memory=f"me{mem}",
    )
    hub_ref_id = _hub_id(resp.body)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
            "AND relation = 'related-to'",
            (hub_ref_id,),
        ).fetchall()
    assert mem in {int(r[0]) for r in rows}


def test_refuses_a_hub_already_in_the_publish_pipeline(store: Store) -> None:
    """`nanopub_reopen` keeps `artifact_type`, so re-typing an existing claim
    row as a hypothesis would assemble the wrong artifact."""
    pa1, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    existing = mint_hub(store, CanonicalClaim(sentence=_SENTENCE, scope={}))
    attach_evidence(
        store,
        hub_ref_id=existing,
        paper_ref_id=pa1,
        role="corroborates",
        meta={"source_handle": f"pc{ch1}"},
        check_retraction=False,
    )
    store.nanopub_create_publish_row(existing, artifact_type="claim")

    with pytest.raises(BadInput, match="already has a nanopub publish row"):
        _handler(store).put(
            title=_SENTENCE,  # same sentence ⇒ converges onto `existing`
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        )


def test_proposal_is_idempotent_on_the_sentence(store: Store) -> None:
    """Re-proposing the same conjecture converges on the one hub rather than
    forking a second — `mint_hub`'s pub_id convergence, and the motivation
    edges dedup with it."""
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    kw: dict[str, Any] = {
        "title": _SENTENCE,
        "hypothesis": True,
        "motivation": "leap",
        "testable_by": "experiment",
        "motivated_by": [f"pc{ch1}", f"pc{ch2}"],
        "llm_models": ["test-model"],
    }
    first = _hub_id(_handler(store).put(**kw).body)
    second = _hub_id(_handler(store).put(**kw).body)
    assert first == second
    assert len(_links(store, first, "motivated-by")) == 2


def test_a_gate_failing_sentence_is_refused_before_anything_is_minted(
    store: Store,
) -> None:
    """The door lints the sentence *before* `mint_hub`. Minting first and
    checking after would strand a hub in the human queue that the proposing
    agent has no permission to delete."""
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    before = store.count_refs(kind="finding")

    with pytest.raises(BadInput, match="fails the mint gates"):
        _handler(store).put(
            # `author-name` — a code a hypothesis still faces. The specimen
            # used to be an epistemic-pair failure, which td244962 retired:
            # the pair asks how a finding was established and a conjecture
            # has no answer, so it no longer applies to this type.
            title="DFT shows Smith 2020 measured a gap of 1.5 eV.",
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        )

    assert store.count_refs(kind="finding") == before


def test_a_hypothesis_need_not_name_an_epistemic_mode(store: Store) -> None:
    """td244962: the epistemic pair is a category error for a conjecture —
    no measurement exists yet, and requiring the sentence to name one makes
    the cheapest way to pass the door a technique that never ran. The
    discriminating experiment goes in `testable_by`, which this door
    already requires."""
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")

    resp = _handler(store).put(
        title="The elastic modulus rises by 12% under uniaxial strain.",
        hypothesis=True,
        motivation="leap",
        testable_by="nanoindentation under matched tip and load",
        motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        llm_models=["test-model"],
    )

    assert _hub_id(resp.body)


def test_refuses_to_converge_onto_a_pre_existing_ordinary_hub(store: Store) -> None:
    """`mint_hub` identifies a hub by sentence+scope and returns an existing
    one WITHOUT applying `extra_meta`. The same property that makes a
    re-proposal a safe no-op would, on a collision with somebody's real
    claim, hang motivation edges and the triage tag on it while leaving it
    unmarked — an established claim mislabelled into the human queue."""
    pa1, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")

    # An ordinary, evidence-bearing hub that never reached a publish row —
    # the common case, so the publish-row guard does not cover it.
    existing = mint_hub(store, CanonicalClaim(sentence=_SENTENCE, scope={}))
    attach_evidence(
        store,
        hub_ref_id=existing,
        paper_ref_id=pa1,
        role="corroborates",
        meta={"source_handle": f"pc{ch1}"},
        check_retraction=False,
    )

    with pytest.raises(BadInput, match="already exists as an ordinary claim hub"):
        _handler(store).put(
            title=_SENTENCE,
            hypothesis=True,
            motivation="leap",
            testable_by="experiment",
            llm_models=["test-model"],
            motivated_by=[f"pc{ch1}", f"pc{ch2}"],
        )

    # Nothing was hung off it: no motivation edges, no triage tag, and its
    # real evidence is untouched.
    assert _links(store, existing, "motivated-by") == []
    assert PROPOSED_TAG not in {t.value for t in store.tags_for(existing)}
    # Evidence runs paper -> hub, so it is inbound; motivation is outbound.
    with store.pool.connection() as conn:
        evidence = conn.execute(
            "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
            "AND relation = 'corroborates'",
            (existing,),
        ).fetchall()
    assert [int(r[0]) for r in evidence] == [pa1]
