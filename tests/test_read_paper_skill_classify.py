"""precis-read-paper's taxonomy step against the machine-edge gate (gr723211).

Step 2 attaches a setup note to its method term with
``link(..., rel='instance-of')``. A plain agent call writes that edge; the
same call under a machine-writer context (a producer origin plus a model
identity) files the classification to the garden ledger instead — the
reply is a judge-reviewed proposal receipt, not a refusal (the 2026-11-19
ruling). Without the evidence contract the call is still refused
``machine-edge-structural``, and the skill has to say all of that by name.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import ChunkInsert
from tests._skill_roots import skill_path

_REFUSAL = "machine-edge-structural"

_MACHINE_META: dict[str, Any] = {
    "model": "test-model",
    "prompt_version": "v1",
    "run_id": "run-1",
    "why": "the methods block names the functional",
}


def _endpoints(store: Store) -> tuple[int, int]:
    setup = store.insert_ref(
        kind="memory", slug=None, title="Setup: DFT PBE+U (VASP)", meta={}
    )
    term = store.insert_ref(kind="taxon", slug=None, title="PBE+U", meta={})
    return int(setup.id), int(term.id)


def _classify(
    runtime: PrecisRuntime, setup: int, term: int, meta: dict[str, Any] | None = None
) -> tuple[str, bool]:
    args: dict[str, Any] = {
        "kind": "memory",
        "id": setup,
        "target": f"taxon:{term}",
        "rel": "instance-of",
    }
    if meta is not None:
        args["meta"] = meta
    return runtime.dispatch_with_status("link", args)


def _proposals(store: Store, target: int) -> list[dict[str, Any]]:
    """Pending garden proposals filed against the ref, decoded."""
    from precis.graph.garden.proposals import _parse_note

    return [
        g
        for rev in store.reviews_for("ref", target)
        if rev.verdict == "proposed" and (g := _parse_note(rev.note))
    ]


def _skill_text() -> str:
    return skill_path("precis-read-paper").read_text(encoding="utf-8")


def _step(text: str, number: int) -> str:
    start = text.index(f"## Step {number} ")
    return text[start : text.index(f"## Step {number + 1} ", start)]


def test_plain_agent_classification_is_written(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    setup, term = _endpoints(store)
    out, error = _classify(runtime_with_store, setup, term)
    assert not error, out
    (edge,) = store.links_for(setup, direction="out", relation="instance-of")
    assert edge.dst_ref_id == term


def test_machine_reader_classification_files_a_proposal(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ruled flow: a machine-context ``link`` call carrying the
    evidence contract returns a proposal receipt — the edge stays
    unwritten and a pending ``relink`` garden row names it for the
    judge."""
    from precis.graph.garden.apply import MergeRefused, apply_relink
    from precis.graph.garden.consumer import consume_llm
    from precis.graph.garden.proposals import pending_proposals
    from precis.utils.llm import router

    setup, term = _endpoints(store)
    paper = store.insert_ref(
        kind="paper", slug="method-paper", title="Methods", meta={}
    )
    passage = "The Hubbard correction was applied to the Fe 3d orbitals."
    (chunk,) = store.chunks.insert_chunks(
        int(paper.id), [ChunkInsert(ord=0, text=passage, meta={})]
    )
    meta = {**_MACHINE_META, "evidence": [f"pc{chunk.id}"]}
    with revision_context(
        "read-paper pass",
        model="test-model",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        out, error = _classify(runtime_with_store, setup, term, meta)
    assert not error, out
    assert "proposal" in out and "review" in out
    assert not store.links_for(setup, direction="out", relation="instance-of")
    (prop,) = _proposals(store, setup)
    assert prop["action"] == "relink"
    payload = prop["payload"]
    assert payload["relation"] == "instance-of"
    assert payload["child"] == setup
    assert payload["parent"] == term

    (pending,) = pending_proposals(store, action="relink")

    def judge(req: router.LlmRequest) -> SimpleNamespace:
        assert passage in req.prompt
        return SimpleNamespace(error=None, text='{"verdict":"approve"}', model="judge")

    monkeypatch.setattr(router, "route", judge)
    assert consume_llm(store, pending).verdict == "approve"

    store.chunks.insert_chunks(
        int(paper.id),
        [ChunkInsert(ord=0, text="No Hubbard correction was applied.", meta={})],
        replace=True,
    )
    stale = consume_llm(store, pending)
    assert stale.verdict == "reject" and stale.retryable
    with pytest.raises(MergeRefused, match="cited passage"):
        apply_relink(store, pending.review_id, actor="garden-consumer")
    assert not store.links_for(setup, direction="out", relation="instance-of")


def test_machine_reader_classification_without_meta_is_refused(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The direct-write restriction stands: the same call missing the
    evidence contract is refused ``machine-edge-structural`` and files
    nothing."""
    setup, term = _endpoints(store)
    with revision_context(
        "read-paper pass",
        model="test-model",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        out, error = _classify(runtime_with_store, setup, term)
    assert error, out
    assert _REFUSAL in out
    assert not store.links_for(setup, direction="out", relation="instance-of")
    assert not _proposals(store, setup)


def test_skill_names_the_door_where_it_shows_the_call() -> None:
    step = _step(_skill_text(), 2)
    assert 'rel="instance-of"' in step
    assert "proposal" in step
    assert _REFUSAL in step


def test_skill_describes_the_receipt_not_a_skip() -> None:
    flat = " ".join(_step(_skill_text(), 2).split())
    assert "skip that `link`" not in flat
    assert "judge" in flat
