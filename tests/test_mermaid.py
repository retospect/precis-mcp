"""The `mermaid` kind (ADR 0057, slice 4).

The node extractor / sanitize / bindings are pure Python and always run;
the mermaidx-backed compile check is guarded with ``importorskip`` so the
gate (which does not bake the [mermaid] extra) stays green. The handler CRUD
and the end-to-end turn run without mermaidx too — ``compile_error`` accepts
when the engine is absent, so the shared core still drives the kind.
"""

from __future__ import annotations

import pytest

from precis.diagram.lang import DiagramLang
from precis.diagram.turn import run_turn
from precis.dispatch import Hub
from precis.mermaid import MERMAID_LANG as LANG

_FLOW = """flowchart TD
  intake[Intake] --> review{Approved?}
  review -->|yes| ship[Ship]
  review -->|no| intake
  ship --> done((Done))"""


# ── MermaidLang (pure) ─────────────────────────────────────────────────────


def test_mermaidlang_conforms_to_the_port() -> None:
    assert isinstance(LANG, DiagramLang)
    assert LANG.kind == "mermaid"
    assert LANG.source_kind == "mermaid_node"
    assert LANG.source_key == "mermaid"
    assert LANG.default_bounds() is None


def test_extract_flowchart_nodes_with_shape_and_topology() -> None:
    els = {e.id: e for e in LANG.elements(_FLOW)}
    assert set(els) == {"intake", "review", "ship", "done"}
    assert els["review"].tag == "diamond"
    assert els["done"].tag == "circle"
    assert els["intake"].coords == "→review"
    assert set(els["review"].coords.lstrip("→").split(",")) == {"ship", "intake"}


def test_extract_sequence_participants() -> None:
    seq = "sequenceDiagram\n  participant A as Alice\n  A->>B: Hi\n  B-->>A: Yo"
    assert [e.id for e in LANG.elements(seq)] == ["A", "B"]
    assert all(e.tag == "participant" for e in LANG.elements(seq))


def test_sanitize_drops_click_directives() -> None:
    src = 'flowchart TD\n  A-->B\n  click A "http://evil" _blank'
    out = LANG.sanitize(src)
    assert "click" not in out
    assert "A-->B" in out


def test_dangling_binding_lint() -> None:
    findings = LANG.lint_bindings(_FLOW, {"intake", "ghost"})
    assert [f.node for f in findings] == ["ghost"]
    assert LANG.lint_bindings(_FLOW, {"intake", "ship"}) == []


def test_compile_valid_and_invalid() -> None:
    pytest.importorskip("mermaidx")
    assert LANG.parse_error(_FLOW) is None
    err = LANG.parse_error("flowchart TD\n  A[[[ -->")
    assert err and "arse error" in err  # mermaid's "Parse error on line …"


# ── handler ────────────────────────────────────────────────────────────────


@pytest.fixture
def mh(store):
    from precis.handlers.mermaid import MermaidHandler

    return MermaidHandler(hub=Hub(store=store))


@pytest.fixture
def diagram(store, mh):
    mh.put(id="flow", title="Pipeline", text=_FLOW)
    return store.get_ref(kind="mermaid", id="flow")


def _target(store) -> str:
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    src, _t = store.create_draft(name="d", title="D", project_ref_id=proj)
    store.add_chunks(ref_id=src.id, chunk_kind="paragraph", text="the intake stage")
    return store.reading_order(src.id)[1].dc


def _source_chunk_id(store, ref_id: int) -> int:
    for c in store.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_node":
            return c.chunk_id
    raise AssertionError("no mermaid_node")


def test_put_creates_and_get_renders(store, mh, diagram) -> None:
    body = mh.get(id="flow").body
    assert "mermaid" in body
    assert "intake[Intake]" in body  # source shown
    # node address handle mn<id> resolves
    node = _source_chunk_id(store, diagram.id)
    from precis.utils import handle_registry

    h = handle_registry.format_handle("mermaid", node, chunk=True)
    assert h.startswith("mn")
    assert mh.get(id=h).body.startswith(h)


def test_link_binds_node_and_get_shows_it(store, mh, diagram) -> None:
    node = _source_chunk_id(store, diagram.id)
    h = _target(store)
    out = mh.link(id="flow", element="intake", target=h)
    assert "bound node 'intake'" in out.body
    got = {(b["element"], b["handle"]) for b in store.element_bindings(node)}
    assert got == {("intake", h)}
    assert "## Bindings" in mh.get(id="flow").body


def test_link_remove_unbinds(store, mh, diagram) -> None:
    node = _source_chunk_id(store, diagram.id)
    h = _target(store)
    mh.link(id="flow", element="intake", target=h)
    mh.link(id="flow", element="intake", mode="remove")
    assert store.element_bindings(node) == []


# ── the shared turn loop drives mermaid ─────────────────────────────────────


def _fixed(**payload):
    return lambda _prompt: dict(payload)


def test_turn_edits_source_and_reconciles_links(store, mh, diagram) -> None:
    node = _source_chunk_id(store, diagram.id)
    h = _target(store)
    new_src = "flowchart TD\n  intake[Intake] --> ship[Ship]"
    res = run_turn(
        LANG,
        store,
        diagram,
        "simplify and bind intake",
        claude_fn=_fixed(
            reply="done",
            mermaid=new_src,
            links=[{"element": "intake", "target": h, "relation": "depicts"}],
        ),
    )
    assert res.changed
    assert "ship[Ship]" in res.svg  # TurnResult.svg holds the source
    got = {(b["element"], b["handle"]) for b in store.element_bindings(node)}
    assert got == {("intake", h)}
