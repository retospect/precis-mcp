"""The ``host_facts`` detector (gr560767): host state prose → machine refs.

Dev-DB fixtures only; no model calls. The spark case — a memory claiming
the host is retired while the machine ref carries its compute role — is
the shape reproduced here, as the spec's first fixture.
"""

from __future__ import annotations

from dataclasses import replace

from precis.dispatch import boot
from precis.graph.garden import candidates as cand
from precis.graph.garden import runner
from precis.graph.garden.apply import apply_rewrite, undo_rewrite
from precis.graph.garden.proposals import (
    MEMORY_APPLY_DETECTORS,
    emit_proposal,
    pending_proposals,
)
from precis.graph.garden.proposer import MEMORY_ACTIONS
from precis.graph.garden.smells import MEMORY_DETECTORS as SMELL_DETECTORS
from precis.handlers.machine import MachineHandler
from precis.store import Store
from precis.utils.handle_registry import try_format
from tests.test_garden_memory import _memory
from tests.test_garden_memory_apply import _body

CLAIM = "AlphaFold3 runs on spark, which is permanently retired."


def _handler(store: Store) -> MachineHandler:
    hub = boot(store=store)
    handler = hub.handlers["machine"]
    assert isinstance(handler, MachineHandler)
    return handler


def _backdate(store: Store, ref_id: int, *, chunks: bool = False) -> None:
    """Deterministic age ordering — same-transaction now() would tie."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '1 day' WHERE ref_id = %s",
            (ref_id,),
        )
        conn.execute(
            "UPDATE revisions SET at = now() - interval '1 day' "
            "WHERE target_kind = 'ref' AND target_id = %s",
            (ref_id,),
        )
        if chunks:
            conn.execute(
                "UPDATE chunks SET created_at = now() - interval '1 day' "
                "WHERE ref_id = %s",
                (ref_id,),
            )


def _machine_ref(store: Store, host: str, *, role: str | None = None) -> int:
    _handler(store).put(id=host, role=role)
    ref = store.get_ref(kind="machine", id=f"ma_{host}")
    assert ref is not None
    _backdate(store, int(ref.id))  # the census fact predates the memory
    return int(ref.id)


def test_host_facts_pass_is_opt_in_and_registered() -> None:
    assert cand.detectors_for(("host-facts",)) == ["host_facts"]
    assert "host_facts" not in cand.detectors_for(("all",))
    assert "host_facts" not in cand.detectors_for(("memory",))
    assert "host_facts" not in cand.detectors_for(("all", "memory"))
    assert "host-facts" in cand.OPT_IN_PASSES
    from precis.workers.job_types.graph_garden import PARAMS_SCHEMA

    enum = PARAMS_SCHEMA["properties"]["pass"]["anyOf"][0]["enum"]
    assert "host-facts" in enum
    # The reviewed apply door re-runs this detector's predicate.
    assert MEMORY_APPLY_DETECTORS["host_facts"] == "host_facts_candidates"
    assert MEMORY_ACTIONS["host_facts"] == ("rewrite",)
    assert "host_facts" in SMELL_DETECTORS
    assert runner._ASK_LLM_TIERS["host_facts"] == "opus"


def test_spark_claim_becomes_a_rewrite_proposal(store: Store) -> None:
    ref_id = _machine_ref(store, "spark", role="compute")
    target = _memory(store, CLAIM)
    found = cand.host_facts_candidates(store)
    assert len(found) == 1
    candidate = found[0]
    assert candidate.detector == "host_facts"
    assert candidate.action == "rewrite"
    assert candidate.target_kind == "ref"
    assert candidate.target_id == target
    handle = try_format("machine", ref_id)
    (host,) = candidate.evidence["hosts"]
    assert host["host"] == "spark"
    assert host["handle"] == handle
    assert host["ref_id"] == ref_id
    assert host["role"] == "compute"
    assert host["newer_than_ref"] is True
    assert host["ref_updated_at"] and host["memory_touched"]
    (sentence,) = host["sentences"]
    assert sentence["text"] == CLAIM
    start, end = sentence["span"]
    assert CLAIM[start:end] == CLAIM
    op = candidate.payload["ops"][0]
    assert op["find"] == CLAIM
    assert op["text"] is None  # the evaluator writes the dated event
    assert op["span"] == sentence["span"]
    assert candidate.payload["base_sha"] == candidate.evidence["content_sha"]
    assert candidate.payload["evidence_sha"] in candidate.key
    # The memory is newer than the ref's fact: the put moves it onto the ref.
    (put,) = candidate.payload["machine_put"]
    assert put["id"] == "spark"
    assert put["args"] == {"role": None}


def test_ref_fact_newer_than_memory_proposes_no_put(store: Store) -> None:
    target = _memory(store, CLAIM)
    _backdate(store, target, chunks=True)  # the memory predates the census
    _machine_ref(store, "spark", role="compute")
    (candidate,) = cand.host_facts_candidates(store)
    (host,) = candidate.evidence["hosts"]
    assert host["newer_than_ref"] is False
    assert "machine_put" not in candidate.payload


def test_dated_or_handle_sentences_are_records_not_claims(store: Store) -> None:
    ref_id = _machine_ref(store, "spark", role="compute")
    handle = try_format("machine", ref_id)
    _memory(
        store,
        "Spark left cluster duty 2026-08-29 (thermal) and rejoined as a "
        f"compute worker 2026-10-15; current role on {handle}.",
    )
    _memory(store, "Spark rejoined the compute pool on 2026-10-15.")
    assert cand.host_facts_candidates(store) == []


def test_hostname_matches_whole_words_only(store: Store) -> None:
    _machine_ref(store, "spark", role="compute")
    _memory(store, "The sparkle effect in the UI is live on any host.")
    _memory(store, "Runs on ma_spark exclusively since the migration.")
    assert cand.host_facts_candidates(store) == []


def test_hosts_without_machine_refs_emit_nothing(store: Store) -> None:
    _machine_ref(store, "spark", role="compute")
    _memory(store, "Pollux carries the database and spark runs compute.")
    (candidate,) = cand.host_facts_candidates(store)
    hosts = candidate.evidence["hosts"]
    assert [h["host"] for h in hosts] == ["spark"]


def test_no_census_no_candidates(store: Store) -> None:
    _memory(store, CLAIM)
    assert cand.host_facts_candidates(store) == []


def test_match_cap_marks_capped(store: Store) -> None:
    _machine_ref(store, "spark", role="compute")
    _memory(store, "\n".join(f"Spark is retired, pass {i}." for i in range(25)))
    (candidate,) = cand.host_facts_candidates(store)
    assert len(candidate.payload["ops"]) == cand.HOST_FACTS_MATCH_CAP
    assert candidate.payload["matches_capped"] is True


def test_apply_round_trip(store: Store) -> None:
    ref_id = _machine_ref(store, "spark", role="compute")
    target = _memory(store, CLAIM)
    candidate = cand.host_facts_candidates(store)[0]
    op = dict(candidate.payload["ops"][0])
    handle = try_format("machine", ref_id)
    op["text"] = (
        "Spark left cluster duty 2026-08-29 (thermal) and rejoined as a "
        f"compute worker 2026-10-15, 932364d0a; current role on {handle}."
    )
    resolved = replace(candidate, payload={**candidate.payload, "ops": [op]})
    review_id = emit_proposal(store, resolved, rationale="dated event plus handle")
    assert review_id is not None
    rec = apply_rewrite(store, review_id, actor="test")
    assert rec.target == target
    assert CLAIM not in _body(store, target)
    assert op["text"] in _body(store, target)
    assert pending_proposals(store, action="rewrite") == []
    # The rewritten sentence carries a date and the handle: the detector
    # must not re-flag its own output.
    assert cand.host_facts_candidates(store) == []
    counts = undo_rewrite(store, rec.rewrite_id, actor="test")
    assert counts["links_restored"] == 0
    assert _body(store, target) == CLAIM


def test_run_detectors_dispatches_host_facts(store: Store) -> None:
    _machine_ref(store, "spark", role="compute")
    _memory(store, CLAIM)
    run = cand.run_detectors(store, passes=("host-facts",), detector_deadline_s=0)
    assert any(c.detector == "host_facts" for c in run.candidates)


def test_evidence_sha_reopens_when_the_census_fact_changes(store: Store) -> None:
    ref_id = _machine_ref(store, "spark", role="compute")
    target = _memory(store, CLAIM)
    first = cand.host_facts_candidates(store)[0]
    _handler(store).put(id="spark", role="storage")
    fresh = cand.host_facts_candidates(store)[0]
    assert fresh.target_id == target
    assert fresh.key != first.key
    (host,) = fresh.evidence["hosts"]
    assert host["role"] == "storage"
    assert host["handle"] == try_format("machine", ref_id)


def test_machine_put_fills_from_the_verified_truth(store: Store) -> None:
    """The reviewer-side put is the machine door, not a garden action: the
    payload names it, the human applies it via put(kind='machine', ...)."""
    _machine_ref(store, "spark", role="compute")
    _memory(store, CLAIM)
    (candidate,) = cand.host_facts_candidates(store)
    (put,) = candidate.payload["machine_put"]
    response = _handler(store).put(id=put["id"], role="storage")
    assert "updated machine ma_spark" in response.body
    ref = store.get_ref(kind="machine", id="ma_spark")
    assert ref is not None and (ref.meta or {}).get("role") == "storage"
    # A census change is new evidence: a fresh proposal replaces the old.
    (fresh,) = cand.host_facts_candidates(store)
    (host,) = fresh.evidence["hosts"]
    assert host["newer_than_ref"] is False
    assert "machine_put" not in fresh.payload
