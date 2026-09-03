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
from typing import Any

import pytest
from mcp.types import CallToolResult

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


def _body(out: Any) -> str:
    """Pull the text body out of a tool result (mirrors test_mcp_args_kwarg.py)."""
    if isinstance(out, CallToolResult):
        return out.content[0].text  # type: ignore[union-attr]
    return out


def _is_error(out: Any) -> bool:
    return isinstance(out, CallToolResult) and bool(out.isError)


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


def test_edit_structure_ops_reach_the_handler_over_the_mcp_door(
    mounted_runtime: PrecisRuntime,
) -> None:
    """``edit(kind='structure', ops=[...])`` through the real MCP callable
    applies the typed op — pinning the forwarded-into-dispatch-payload half
    of the 2026-08-31 ``ops=``/``args=`` edit wiring (which retired the
    ("structure","edit","ops")/("structure","edit","args") ratchet entries).
    Before it, ops= reached the handler only via the lenient ``__extras__``
    channel; a strict-schema client's edit silently no-opped."""
    tools_core.put(
        kind="structure",
        id="ops-door-test",
        text=(
            '{"cell": {"a": 10, "b": 10, "c": 10, '
            '"pbc": [false, false, false]}, "ops": ['
            '{"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]}]}'
        ),
    )

    out = tools_core.edit(
        kind="structure",
        id="ops-door-test",
        ops=[{"op": "add_atom", "element": "O", "frac": [0.6, 0.5, 0.5]}],
    )

    # The edited TOC reflects the op having been applied: two atoms, the O
    # among them — a dropped ops= would leave the design at one C atom.
    assert "aO1" in out, out


# ---------------------------------------------------------------------------
# gr301897: tag()'s missing meta= and edit()'s silently-swallowed meta=
# ---------------------------------------------------------------------------


def test_tag_todo_meta_reaches_the_handler_over_the_mcp_door(
    mounted_runtime: PrecisRuntime,
    store: Store,
) -> None:
    """``tag(kind='todo', id=…, meta={…})`` through the real MCP callable
    promotes an allowlisted key (``llm_tier``) — not the raw ``tag() got an
    unexpected keyword argument 'meta'`` TypeError a missing tool-layer
    meta= produced before the fix. ``TodoHandler.tag`` already implemented
    the allowlisted promotion (``guards.check_meta_keys_promotable`` +
    ``check_llm_tier_meta``); only this door was missing."""
    tools_core.put(kind="todo", text="unblock the plan_tick crash loop")
    ref = store.list_refs(kind="todo", limit=1)[0]

    out = tools_core.tag(kind="todo", id=ref.id, meta={"llm_tier": "opus"})

    assert not _is_error(out), _body(out)
    assert f"id={ref.id}" in _body(out)
    live = store.get_ref(kind="todo", id=ref.id)
    assert live is not None
    assert live.meta.get("llm_tier") == "opus"


def test_edit_pres_meta_reaches_the_handler_over_the_mcp_door(
    mounted_runtime: PrecisRuntime,
    store: Store,
) -> None:
    """``edit(kind='pres', id=…, meta={…})`` through the real MCP callable
    lands the metadata patch — ``PresentationHandler.edit`` has always
    accepted ``meta=`` (BibTeX attribution fields), but the tool-layer
    ``edit()`` never declared or forwarded it (a pre-existing
    ``("pres","edit","meta")`` gap in the kwarg-parity ratchet, closed
    alongside gr301897)."""
    tools_core.put(kind="pres", id="deck-meta-door-test", text="Title slide")

    out = tools_core.edit(
        kind="pres", id="deck-meta-door-test", meta={"venue": "demo day"}
    )

    assert not _is_error(out), _body(out)
    ref = store.get_ref(kind="pres", id="deck-meta-door-test")
    assert ref is not None
    assert ref.meta.get("venue") == "demo day"


def test_edit_todo_meta_rejected_loudly_not_swallowed(
    mounted_runtime: PrecisRuntime,
    store: Store,
) -> None:
    """``edit(kind='todo', id=…, meta={…})`` must fail loudly — ``TodoHandler
    .edit`` doesn't accept ``meta=`` at all (its meta is set via ``tag()``'s
    allowlisted promotion instead), so before the fix this reached the
    handler's ``**_kw`` catch-all and vanished with a bare "success" body
    and no meta write (gr301897, symptom 2 — worse than the tag() TypeError
    because nothing signalled the drop)."""
    tools_core.put(kind="todo", text="unblock the plan_tick crash loop")
    ref = store.list_refs(kind="todo", limit=1)[0]

    out = tools_core.edit(kind="todo", id=ref.id, meta={"llm_tier": "opus"})

    assert _is_error(out), _body(out)
    body = _body(out)
    assert "[error:BadInput]" in body
    assert "meta" in body
    live = store.get_ref(kind="todo", id=ref.id)
    assert live is not None
    assert live.meta.get("llm_tier") != "opus"
