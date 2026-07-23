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


# ── per-grammar node extraction (task 4: bind on every node-bearing type) ──

_CLASS = """classDiagram
  class Animal {
    +String name
    +eat()
  }
  Animal <|-- Dog
  Animal <|-- Cat
  Dog --> Bone : chews
  Customer "1" --> "*" Ticket : raises
  Vehicle : +int wheels"""

_ER = """erDiagram
  CUSTOMER ||--o{ ORDER : places
  ORDER ||--|{ LINE-ITEM : contains
  CUSTOMER {
    string name
    string custId
  }
  PRODUCT }o--o{ ORDER : in"""

_REQ = """requirementDiagram
  requirement test_req {
    id: 1
    text: the test text.
    risk: high
    verifymethod: test
  }
  element test_entity {
    type: simulation
  }
  test_entity - satisfies -> test_req"""

_STATE = """stateDiagram-v2
  [*] --> Still
  Still --> Moving : go
  Moving --> Crash
  Crash --> [*]
  state Fork {
    [*] --> State2
  }
  state "Named desc" as NS"""

_MINDMAP = """mindmap
  root((Big idea))
    Origins
      Long history
    id1[Research]"""

_GIT = """gitGraph
  commit id: "Alpha"
  branch develop
  commit id: "Beta"
  checkout main
  merge develop
  cherry-pick id: "Beta\""""


def test_extract_class_nodes_declarations_relations_and_shorthand() -> None:
    els = {e.id: e for e in LANG.elements(_CLASS)}
    # a declared class, both relation endpoints, and the `Foo : member` form
    assert set(els) == {"Animal", "Dog", "Cat", "Bone", "Customer", "Ticket", "Vehicle"}
    assert all(e.tag == "class" for e in els.values())
    assert set(els["Animal"].coords.lstrip("→").split(",")) == {"Dog", "Cat"}
    # `"1" --> "*"` cardinality labels are stripped, not read as ids
    assert els["Customer"].coords == "→Ticket"


def test_extract_er_entities_across_cardinality_ops() -> None:
    els = {e.id: e for e in LANG.elements(_ER)}
    assert set(els) == {"CUSTOMER", "ORDER", "LINE-ITEM", "PRODUCT"}  # hyphen id kept
    assert all(e.tag == "entity" for e in els.values())
    assert els["CUSTOMER"].coords == "→ORDER"
    assert els["ORDER"].coords == "→LINE-ITEM"


def test_extract_requirement_nodes_and_relation_direction() -> None:
    els = {e.id: e for e in LANG.elements(_REQ)}
    assert set(els) == {"test_req", "test_entity"}
    assert els["test_req"].tag == "requirement"
    assert els["test_entity"].tag == "element"
    assert els["test_entity"].coords == "→test_req"  # `entity - satisfies -> req`


def test_extract_state_nodes_excludes_pseudostates() -> None:
    ids = {e.id for e in LANG.elements(_STATE)}
    assert ids == {"Still", "Moving", "Crash", "Fork", "State2", "NS"}
    assert "[*]" not in ids  # start/end pseudo-states are never bindable
    els = {e.id: e for e in LANG.elements(_STATE)}
    assert set(els["Moving"].coords.lstrip("→").split(",")) == {"Crash"}


def test_state_name_starting_with_keyword_keeps_its_transition() -> None:
    # `State1` starts with "state" but is a transition, not a `state Foo` decl —
    # its out-edge must survive (regression: the decl branch used to swallow it)
    els = {e.id: e for e in LANG.elements("stateDiagram-v2\n  State1 --> Idle")}
    assert set(els) == {"State1", "Idle"}
    assert els["State1"].coords == "→Idle"


def test_class_named_like_a_directive_still_binds() -> None:
    # a class NAMED `Note` / `Link` (mermaid directive keywords) in a relation
    src = "classDiagram\n  Note <|-- Footnote\n  Link --> Anchor"
    els = {e.id: e for e in LANG.elements(src)}
    assert set(els) == {"Note", "Footnote", "Link", "Anchor"}
    assert els["Link"].coords == "→Anchor"
    # but real directive lines carry no node
    assert "note" not in {
        e.id for e in LANG.elements('classDiagram\n  class A\n  note "a: b"')
    }


def test_extract_mindmap_ids_by_indentation_tree() -> None:
    els = {e.id: e for e in LANG.elements(_MINDMAP)}
    # explicit id (`root`, `id1`) where given, slug of the text otherwise
    assert set(els) == {"root", "origins", "long-history", "id1"}
    assert set(els["root"].coords.lstrip("→").split(",")) == {"origins", "id1"}
    assert els["origins"].coords == "→long-history"


def test_extract_gitgraph_branches_and_tagged_commits() -> None:
    els = {e.id: e for e in LANG.elements(_GIT)}
    assert set(els) == {"Alpha", "develop", "Beta"}  # branch + id:-tagged commits
    assert els["develop"].tag == "branch"
    assert els["Alpha"].tag == "commit"


@pytest.mark.parametrize(
    "src",
    [
        "journey\n  title Flow\n  section Discover\n    Land: 4: User",
        "timeline\n  title History\n  2021 : launch\n  2022 : growth",
        'xychart-beta\n  title "Sales"\n  x-axis [jan, feb]\n  bar [5, 6]',
        "quadrantChart\n  title Reach\n  x-axis Low --> High\n  A: [0.3, 0.6]",
    ],
)
def test_data_series_diagrams_have_no_bindable_nodes(src: str) -> None:
    # journey/timeline/xychart/quadrant carry data rows, not stable node ids —
    # returning [] keeps lint_bindings from false-positiving on them
    assert LANG.elements(src) == []


def test_lint_bindings_now_clean_on_non_flowchart_types() -> None:
    # the bug task 4 fixes: these ids used to be invisible → false dangling
    assert LANG.lint_bindings(_CLASS, {"Animal", "Ticket"}) == []
    assert LANG.lint_bindings(_ER, {"CUSTOMER", "LINE-ITEM"}) == []
    assert LANG.lint_bindings(_STATE, {"Moving", "Fork"}) == []
    assert [f.node for f in LANG.lint_bindings(_ER, {"ORDER", "ghost"})] == ["ghost"]


@pytest.mark.parametrize(
    "src",
    [
        _ER,
        _REQ,
        _STATE,
        _MINDMAP,
        _GIT,
        # classDiagram renders, but quoted cardinality trips a QuickJS gap
        # (structuredClone) — exercise the plain form here; extraction of the
        # cardinality form is covered purely above.
        "classDiagram\n  Animal <|-- Dog\n  Dog --> Bone : chews",
    ],
)
def test_extraction_targets_render_valid_sources(src: str) -> None:
    pytest.importorskip("mermaidx")
    assert (
        LANG.parse_error(src) is None
    )  # the sample is real mermaid the engine renders
    assert LANG.elements(src)  # and we pull at least one bindable node from it


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


# ── MCP tool surface: vocab=/notes=/viewbox= on edit, element= on link ──────
#
# Regression for the OPEN-ITEMS "MCP vocab/notes/element plumbing gap" bug:
# ``DiagramHandler.edit``/``.link`` (exercised above) always accepted these
# kwargs, but the *exposed* MCP ``edit``/``link`` tool functions in
# ``precis.tools.core`` had a fixed parameter list that didn't declare them —
# so a real MCP client calling ``edit(kind='mermaid', vocab=...)`` had the
# kwarg silently stripped before it ever reached the handler. These tests go
# through the actual tool-dispatch path (``precis.tools.core.edit``/``.link``)
# rather than calling the handler directly, so a regression at the tool-
# signature layer fails here even though the handler-level tests above stay
# green.


def test_mcp_edit_tool_persists_vocab_and_notes(
    monkeypatch, store, runtime_with_store, diagram
) -> None:
    import precis.tools.core as core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    out = core.edit(kind="mermaid", id="flow", vocab="intake = the ingress stage")
    assert isinstance(out, str) and "vocabulary" in out
    out = core.edit(kind="mermaid", id="flow", notes="draft v2, needs review")
    assert isinstance(out, str) and "notes" in out

    body = core.get(kind="mermaid", id="flow")
    assert isinstance(body, str)
    assert "intake = the ingress stage" in body
    assert "draft v2, needs review" in body


def test_mcp_link_tool_persists_element_binding(
    monkeypatch, store, runtime_with_store, diagram
) -> None:
    import precis.tools.core as core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    h = _target(store)
    node = _source_chunk_id(store, diagram.id)
    out = core.link(kind="mermaid", id="flow", element="intake", target=h)
    assert isinstance(out, str) and "bound" in out
    got = {(b["element"], b["handle"]) for b in store.element_bindings(node)}
    assert got == {("intake", h)}


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
