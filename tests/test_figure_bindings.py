"""Figure element→chunk bindings + prepared context (ADR 0057, slice 2).

The pure SVG helpers (elements / coords / dangling lint), the context
assembler, the turn `links` reconcile, and the handler link(element=) surface.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.figure.context import render_diagram_context
from precis.figure.svg import Element, elements, lint_bindings
from precis.figure.turn import build_prompt, run_turn
from precis.handlers.figure import FigureHandler

_FACE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<circle id="face" cx="50" cy="50" r="30"/>'
    '<rect id="mouth" x="40" y="60" width="20" height="6"/></svg>'
)
_NO_FACE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect id="mouth" x="40" y="60" width="20" height="6"/></svg>'
)


# ── pure svg helpers ──────────────────────────────────────────────────────


def test_elements_extracts_ids_and_coords() -> None:
    els = {e.id: e for e in elements(_FACE)}
    assert set(els) == {"face", "mouth"}
    assert els["face"] == Element(id="face", tag="circle", coords="cx50 cy50 r30")
    assert els["mouth"] == Element(id="mouth", tag="rect", coords="x40 y60 w20 h6")


def test_elements_empty_on_garbage() -> None:
    assert elements("<svg><oops") == []


def test_lint_bindings_flags_only_missing_ids() -> None:
    assert lint_bindings(_FACE, {"face", "mouth"}) == []
    findings = lint_bindings(_FACE, {"face", "ghost"})
    assert len(findings) == 1
    assert findings[0].kind == "binding"
    assert findings[0].node == "ghost"
    assert lint_bindings(_FACE, set()) == []


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fh(store):
    return FigureHandler(hub=Hub(store=store))


@pytest.fixture
def fig(store, fh):
    fh.put(id="m", title="M", text=_FACE)
    return store.get_ref(kind="figure", id="m")


def _target(store) -> str:
    """A draft chunk to bind to; returns its dc<id> handle."""
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    src, _t = store.create_draft(name="parts", title="Parts", project_ref_id=proj)
    store.add_chunks(
        ref_id=src.id, chunk_kind="paragraph", text="the face is a green circle"
    )
    return store.reading_order(src.id)[1].dc


def _source_chunk_id(store, ref_id: int) -> int:
    for c in store.reading_order(ref_id, kind="figure"):
        if c.chunk_kind == "figure_node":
            return c.chunk_id
    raise AssertionError("no figure_node")


def _fixed(**payload):
    return lambda _prompt: dict(payload)


# ── context assembler ─────────────────────────────────────────────────────


def test_render_diagram_context_lists_elements_and_body(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    store.bind_element(node_chunk_id=node, element="face", target=h)

    ctx = render_diagram_context(store, node, _FACE)
    assert "Diagram elements ↔ linked context" in ctx
    assert "face" in ctx and h in ctx and "(depicts)" in ctx
    assert "cx50 cy50 r30" in ctx  # geometry surfaced
    assert "Linked chunk bodies" in ctx
    assert "> the face is a green circle" in ctx  # body quoted


def test_render_diagram_context_empty_without_bindings(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    assert render_diagram_context(store, node, _FACE) == ""


def test_render_diagram_context_marks_dangling(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    store.bind_element(node_chunk_id=node, element="ghost", target=h)
    ctx = render_diagram_context(store, node, _FACE)
    assert "dangling" in ctx


def test_build_prompt_carries_context() -> None:
    p = build_prompt(
        message="x",
        svg="<svg/>",
        vocab="",
        findings=[],
        viewbox=(0.0, 0.0, 100.0, 100.0),
        context="## Diagram elements ↔ linked context\n- face → dc7",
    )
    assert "Diagram elements ↔ linked context" in p
    assert '"links"' in p  # the contract now asks for links


# ── turn reconcile ────────────────────────────────────────────────────────


def test_turn_creates_bindings_from_links(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    res = run_turn(
        store,
        fig,
        "bind the face",
        claude_fn=_fixed(
            reply="bound",
            svg=_FACE,
            links=[{"element": "face", "target": h, "relation": "depicts"}],
        ),
    )
    got = {(b["element"], b["handle"]) for b in store.element_bindings(node)}
    assert got == {("face", h)}
    assert {(b["element"], b["handle"]) for b in res.bindings} == {("face", h)}


def test_turn_without_links_leaves_bindings(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    store.bind_element(node_chunk_id=node, element="face", target=h)
    # a chat-only turn (no svg, no links) must not disturb bindings
    run_turn(store, fig, "hi", claude_fn=_fixed(reply="hello"))
    assert {b["element"] for b in store.element_bindings(node)} == {"face"}


def test_turn_empty_links_clears_bindings(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    store.bind_element(node_chunk_id=node, element="face", target=h)
    run_turn(store, fig, "unbind all", claude_fn=_fixed(reply="cleared", links=[]))
    assert store.element_bindings(node) == []


def test_turn_dangling_binding_surfaces_in_findings(store, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    store.bind_element(node_chunk_id=node, element="face", target=h)
    # the model removes the <circle id="face"> but keeps the binding
    res = run_turn(
        store, fig, "drop the face", claude_fn=_fixed(reply="dropped", svg=_NO_FACE)
    )
    kinds = {(f.kind, f.node) for f in res.findings}
    assert ("binding", "face") in kinds


# ── handler surface ───────────────────────────────────────────────────────


def test_handler_link_binds_and_get_shows_it(store, fh, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    out = fh.link(id="m", element="face", target=h)
    assert "bound element 'face'" in out.body

    got = {(b["element"], b["handle"]) for b in store.element_bindings(node)}
    assert got == {("face", h)}

    rendered = fh.get(id="m").body
    assert "## Bindings" in rendered
    assert f"face → {h}" in rendered


def test_handler_link_remove_unbinds(store, fh, fig) -> None:
    node = _source_chunk_id(store, fig.id)
    h = _target(store)
    fh.link(id="m", element="face", target=h)
    out = fh.link(id="m", element="face", mode="remove")
    assert "unbound" in out.body
    assert store.element_bindings(node) == []
