"""precis-read-paper's taxonomy step against the machine-edge gate (gr723211).

Step 2 attaches a setup note to its method term with
``link(..., rel='instance-of')``. A plain agent call writes that edge; the
same call under a machine-writer context (a producer origin plus a model
identity) is refused ``machine-edge-structural``, whatever evidence and
annotation it carries. The skill has to say so by the refusal's own name,
and must not promise the reader a proposal the handler never returns.
"""

from __future__ import annotations

from typing import Any

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import revision_context
from tests._skill_roots import skill_path

_REFUSAL = "machine-edge-structural"

_MACHINE_META: dict[str, Any] = {
    "model": "test-model",
    "prompt_version": "v1",
    "run_id": "run-1",
    "why": "the methods block names the functional",
    "evidence": ["pc1"],
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


def test_machine_reader_classification_is_refused(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    setup, term = _endpoints(store)
    with revision_context(
        "read-paper pass",
        model="test-model",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        out, error = _classify(runtime_with_store, setup, term, dict(_MACHINE_META))
    assert error, out
    assert _REFUSAL in out
    assert not store.links_for(setup, direction="out", relation="instance-of")


def test_skill_names_the_refusal_where_it_shows_the_call() -> None:
    step = _step(_skill_text(), 2)
    assert 'rel="instance-of"' in step
    assert _REFUSAL in step


def test_skill_promises_no_proposal_for_a_machine_classification() -> None:
    flat = " ".join(_skill_text().split())
    assert "`instance-of` becomes a gardener proposal" not in flat
