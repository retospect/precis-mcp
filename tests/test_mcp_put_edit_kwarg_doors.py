"""Regression: the ``wants=``/``provenance=`` and ``doi=``/``arxiv=`` doors
must actually open over the ``tools/core.py`` MCP boundary — not just
against the handler directly.

gr262482 / gr250273: ``FindingHandler.put`` has always fully implemented
acquisition mode (``wants=``/``provenance=``) and ``PaperHandler.edit`` has
always fully implemented the ``doi=``/``arxiv=`` identifier upgrade — but
``tools/core.py``'s ``put``/``edit`` never declared or forwarded those
kwargs, so a strict-schema MCP client stripped them before the handler ever
saw them. A test that calls ``FindingHandler.put``/``PaperHandler.edit``
directly (as ``tests/test_finding.py`` does for the acquisition-mode
*behaviour*) never exercises that boundary and would have stayed green
through the whole life of the bug. These round-trip through
``tools_core.put`` / ``tools_core.edit`` themselves — the actual callables
FastMCP wires up — against a real store-backed runtime, so both the
declared-in-signature half AND the forwarded-into-dispatch-payload half of
the fix are pinned together.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.tools import core as tools_core


@pytest.fixture
def mounted_runtime(runtime_with_store: PrecisRuntime) -> Iterator[PrecisRuntime]:
    """Mount a store-backed runtime onto ``tools.core._runtime`` so
    ``tools_core.put`` / ``tools_core.edit`` run for real, exactly the way
    FastMCP invokes them — mirrors ``test_mcp_args_kwarg.py``'s
    ``server_runtime`` fixture."""
    tools_core._runtime = runtime_with_store
    try:
        yield runtime_with_store
    finally:
        tools_core._runtime = None


def _mint_provenance_handle(store: Store) -> str:
    """A live ``me<id>`` memory handle for ``provenance=`` to resolve
    against — acquisition mode requires a real ref/chunk handle, not a
    placeholder."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title="provenance note", meta={}, conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text="the note this claim came from",
                    meta={"chunk_kind": "memory_body"},
                )
            ],
            conn=conn,
        )
    return f"me{ref.id}"


def test_put_finding_acquisition_mode_reaches_the_handler_over_the_mcp_door(
    mounted_runtime: PrecisRuntime,
    store: Store,
) -> None:
    """``put(kind='finding', wants=..., provenance=...)`` through the real
    MCP callable mints an acquisition-mode finding — not the "requires
    cited_in=" error a dropped ``wants=`` produced before the fix."""
    provenance = _mint_provenance_handle(store)

    out = tools_core.put(
        kind="finding",
        title="DFT predicts a 12% modulus rise under uniaxial strain.",
        body=(
            "Claim: DFT predicts a 12% modulus rise under uniaxial strain. "
            "Setup: pending grounding from the wanted paper below."
        ),
        wants=[{"doi": "10.1234/acquire-door-test"}],
        provenance=provenance,
    )

    assert "requires cited_in" not in out
    assert "STATUS:acquiring" in out
    assert "awaiting evidence from 1 paper" in out
    m = re.search(r"created finding id=(\d+)", out)
    assert m is not None, out
    ref_id = int(m.group(1))
    tags = {str(t) for t in store.tags_for(ref_id)}
    assert "STATUS:acquiring" in tags


def test_edit_paper_doi_upgrades_a_title_only_stub_over_the_mcp_door(
    mounted_runtime: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``put(kind='paper', title=...)`` mints a title-only stub;
    ``edit(kind='paper', id=..., doi=...)`` through the real MCP callable
    then upgrades it — the repair path that avoided pa262445/pa262447/
    pa262454/pa262456 becoming duplicate stubs before the fix."""
    # No network round-trip: the doi-edit-risk note does a best-effort
    # Crossref lookup after commit (paper.py::_doi_edit_warning) — swallow
    # it to a clean miss so this test is fast and network-independent; the
    # warning text itself isn't what this test is pinning.
    import precis.ingest.crossref as crossref_mod

    monkeypatch.setattr(crossref_mod, "lookup_crossref", lambda doi, mailto="": None)

    mint_out = tools_core.put(kind="paper", title="A Title-Only Paper Stub")
    m = re.search(r"paper id=(\d+)", mint_out)
    assert m is not None, mint_out
    ref_id = int(m.group(1))

    out = tools_core.edit(kind="paper", id=ref_id, doi="10.1234/upgrade-door-test")

    assert f"updated paper id={ref_id}" in out
    assert "doi" in out
    identifiers = store.identifiers_for_refs([ref_id]).get(ref_id, {})
    assert identifiers.get("doi") == "10.1234/upgrade-door-test"
