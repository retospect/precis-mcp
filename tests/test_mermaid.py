"""The `mermaid` kind.

The node extractor / sanitize / bindings are pure Python and always run;
the mermaidx-backed compile check is guarded with ``importorskip`` so the
gate (which does not bake the [mermaid] extra) stays green. The handler CRUD
and the end-to-end turn run without mermaidx too — ``compile_error`` accepts
when the engine is absent, so the shared core still drives the kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from precis.diagram.lang import DiagramLang
from precis.diagram.turn import run_turn
from precis.dispatch import Hub
from precis.mermaid import MERMAID_LANG as LANG

if TYPE_CHECKING:
    from precis.handlers.mermaid import MermaidHandler

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


@pytest.mark.parametrize(
    "label",
    ['"x pa3 (y)"', '"`first line\npa3 [text] --> ghost\nlast line`"'],
)
def test_extract_flowchart_ignores_quoted_label_text(label: str) -> None:
    src = f'flowchart TD\n  %% unmatched "\n  a[{label}] --> b'
    nodes = LANG.elements(src)
    assert [node.id for node in nodes] == ["a", "b"]
    assert nodes[0].tag == "rect"
    assert nodes[0].coords == "→b"
    assert [f.node for f in LANG.lint_bindings(src, {"a", "b", "pa3"})] == ["pa3"]


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


# ── unsupported diagram types (gr311345) ────────────────────────────────────
#
# ``precis-mermaid-unsupported`` used to document gantt/pie/sankey-beta/
# C4Context/block-beta as types that "will validate-fail" on write, but
# nothing checked this at the put layer — the engine-absent dark path
# accepted anything unconditionally. Meanwhile the mermaidx>=0.9 bump quietly
# fixed gantt/pie/C4Context, leaving that blanket claim stale — only
# sankey-beta/block-beta genuinely still fail to compile. The put-layer
# check (added here) fires without ``mermaidx`` installed (it's a
# source-prefix check, run before the engine is even looked up) — no
# ``importorskip`` on those; the "previously claimed unsupported… now
# compile" pair below needs the real engine to lock in the correction.


@pytest.mark.parametrize(
    "src,label",
    [
        ("sankey-beta\n  A,B,10", "sankey-beta"),
        ("block-beta\n  columns 1\n  a", "block-beta"),
    ],
)
def test_unsupported_type_detected(src: str, label: str) -> None:
    from precis.mermaid.mermaid import unsupported_type

    assert unsupported_type(src) == label


def test_unsupported_type_none_for_supported_types() -> None:
    from precis.mermaid.mermaid import unsupported_type

    assert unsupported_type(_FLOW) is None
    assert unsupported_type("journey\n  title A\n  section S\n  A: 5: Me") is None
    # gr311345: these used to be on the unsupported list; mermaidx>=0.9
    # renders them fine, so the put-layer check must not reject them.
    assert unsupported_type('pie title Share\n  "A" : 40\n  "B" : 60') is None
    assert (
        unsupported_type(
            "gantt\n  title Schedule\n  section S\n  Task1: 2024-01-01, 3d"
        )
        is None
    )
    assert unsupported_type('C4Context\n  Person(a, "A")') is None


def test_compile_error_rejects_unsupported_type_without_engine() -> None:
    """The rejection is deterministic — it fires even when ``mermaidx`` is
    not importable (the container-gate dark path), because the check runs
    before the engine is looked up at all."""
    err = LANG.parse_error("block-beta\n  columns 1\n  a")
    assert err is not None
    assert "block-beta" in err
    assert "not supported" in err
    # Names the supported set, per gr311345 — gantt/pie/C4Context included
    # now that mermaidx>=0.9 actually renders them.
    assert "flowchart" in err
    assert "gantt" in err
    assert "precis-mermaid-unsupported" in err


def test_put_rejects_block_diagram(store, mh) -> None:
    """End-to-end: put(kind='mermaid', text='block-beta …') must raise, not
    silently create the ref (gr311345)."""
    from precis.errors import BadInput

    with pytest.raises(BadInput) as exc_info:
        mh.put(id="chart", title="Chart", text="block-beta\n  columns 1\n  a")
    assert "block-beta" in str(exc_info.value)
    assert store.get_ref(kind="mermaid", id="chart") is None


# ── gr311345: precis-mermaid-unsupported's list vs the real engine ─────────
#
# put()/edit() already run the real mermaidx engine at write time
# (DiagramHandler._validate_source → LANG.parse_error), so the put-layer
# check above is a fast-path in front of it, not a replacement — these lock
# in which types the *current* pinned engine actually accepts/rejects, so a
# future mermaidx bump/regression (in either direction) is caught here
# instead of by a stale doc.


@pytest.mark.parametrize(
    "src",
    [
        'pie title Pets\n  "Dogs" : 40\n  "Cats" : 60',
        "gantt\n    title A Gantt Diagram\n    dateFormat YYYY-MM-DD\n"
        "    section Section\n    A task          :a1, 2014-01-01, 30d",
        'C4Context\n    Person(customerA, "Banking Customer A", "desc")\n'
        '    System(SystemAA, "Internet Banking System", "desc")\n'
        '    Rel(customerA, SystemAA, "Uses")',
    ],
)
def test_previously_claimed_unsupported_types_now_compile(src: str) -> None:
    pytest.importorskip("mermaidx")
    assert LANG.parse_error(src) is None


@pytest.mark.parametrize(
    "src",
    [
        "sankey-beta\n\nAgricultural,Bio-conversion,124.729\n"
        "Bio-conversion,Liquid,42.6",
        "block-beta\ncolumns 3\na b c",
    ],
)
def test_genuinely_unsupported_types_still_fail_to_compile(src: str) -> None:
    pytest.importorskip("mermaidx")
    assert LANG.parse_error(src) is not None


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
    src, _t = store.drafts.create_draft(name="d", title="D", project_ref_id=proj)
    store.drafts.add_chunks(
        ref_id=src.id, chunk_kind="paragraph", text="the intake stage"
    )
    return store.drafts.reading_order(src.id)[1].dc


def _source_chunk_id(store, ref_id: int) -> int:
    for c in store.drafts.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_node":
            return c.chunk_id
    raise AssertionError("no mermaid_node")


def test_put_rejects_genuinely_unsupported_diagram_type(store, mh) -> None:
    # gr311345: put already runs the real engine at write time — a source the
    # engine truly can't render (sankey-beta) must raise, not silently create.
    pytest.importorskip("mermaidx")
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        mh.put(
            id="flow-sankey",
            title="Flow",
            text="sankey-beta\n\nAgricultural,Bio-conversion,124.729",
        )
    assert store.get_ref(kind="mermaid", id="flow-sankey") is None


def test_put_accepts_pie_diagram_now_supported_by_the_engine(store, mh) -> None:
    # gr311345: pie used to be on the "unsupported" skill list; mermaidx>=0.9
    # renders it fine, so put must succeed (this was reported as a "silent"
    # success — it's correct behaviour, not a validation gap).
    pytest.importorskip("mermaidx")
    out = mh.put(
        id="pets",
        title="Pets",
        text='pie title Pets\n  "Dogs" : 40\n  "Cats" : 60',
    )
    assert "created mermaid 'pets'" in out.body
    assert store.get_ref(kind="mermaid", id="pets") is not None


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
    got = {(b["element"], b["handle"]) for b in store.drafts.element_bindings(node)}
    assert got == {("intake", h)}
    assert "## Bindings" in mh.get(id="flow").body


def test_link_remove_unbinds(store, mh, diagram) -> None:
    node = _source_chunk_id(store, diagram.id)
    h = _target(store)
    mh.link(id="flow", element="intake", target=h)
    mh.link(id="flow", element="intake", mode="remove")
    assert store.drafts.element_bindings(node) == []


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
    got = {(b["element"], b["handle"]) for b in store.drafts.element_bindings(node)}
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
    got = {(b["element"], b["handle"]) for b in store.drafts.element_bindings(node)}
    assert got == {("intake", h)}


def test_delete_without_id_names_the_list_then_delete(mh: MermaidHandler) -> None:
    """M3 burn-down (gr494831): delete without id= refuses with next=."""
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="requires id=") as exc:
        mh.delete()
    assert exc.value.next is not None
    assert "get(kind='mermaid')" in str(exc.value.next)
    assert "delete(kind='mermaid', id=" in str(exc.value.next)


def test_missing_slug_hint_names_a_supported_call(mh: MermaidHandler) -> None:
    # gr793755: the kind has no search verb — the NotFound hint must not send
    # the agent to search(kind='mermaid'); get without id lists the diagrams.
    from precis.errors import NotFound

    with pytest.raises(NotFound) as exc:
        mh.get(id="no-such-diagram")
    nxt = str(exc.value.next)
    assert "search(" not in nxt
    assert "get(kind='mermaid')" in nxt


def test_missing_slug_suggests_nearest_live_slugs(store, mh, diagram) -> None:
    """gr797061: a miss carries close-match options — a misremembered or
    renamed slug lands on a live diagram instead of being retried."""
    from precis.errors import NotFound

    with pytest.raises(NotFound) as exc:
        mh.get(id="flo")
    assert exc.value.options is not None
    assert "flow" in exc.value.options


def test_deleted_slug_is_gone_not_notfound(store, mh, diagram) -> None:
    """gr797061: a deleted diagram's slug read as a plain NotFound —
    indistinguishable from a typo, so a stale reference kept being
    reissued. ``Gone`` names the deletion; the caller drops the pointer."""
    from precis.errors import Gone

    mh.delete(id="flow")
    with pytest.raises(Gone, match="was deleted"):
        mh.get(id="flow")


def test_deleted_slug_record_handle_is_gone(store, mh, diagram) -> None:
    """The same tombstone reached through the ``mm<id>`` record handle."""
    from precis.errors import Gone

    handle = f"mm{diagram.id}"
    mh.delete(id="flow")
    with pytest.raises(Gone, match="was deleted"):
        mh.get(id=handle)


def test_edit_and_redelete_on_a_tombstone_raise_gone(store, mh, diagram) -> None:
    from precis.errors import Gone

    mh.delete(id="flow")
    with pytest.raises(Gone):
        mh.edit(id="flow", text="flowchart TD\n  a --> b")
    with pytest.raises(Gone):
        mh.delete(id="flow")


@pytest.mark.usefixtures("diagram")
def test_slug_view_budget_hint_names_supported_calls(mh: MermaidHandler) -> None:
    resp = mh.get(id="flow")
    assert resp.result_budget_hint is not None
    assert "get(kind='mermaid', id='<node handle>')" in resp.result_budget_hint
    assert "get(kind='mermaid')" in resp.result_budget_hint
    assert "search(" not in resp.result_budget_hint


# ── gr793934: the render order + the node-handle dead end ───────────────────


def _notes_handle(store, ref_id: int) -> str:
    for c in store.drafts.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_notes":
            return c.dc
    raise AssertionError("no mermaid_notes chunk")


def test_render_puts_lints_and_source_ahead_of_the_prose(store, mh, diagram) -> None:
    """gr793934: the byte cap cuts from the bottom — vocab/notes/bindings
    used to render ahead of the source (and lints last of all), so a long
    notes doc hid both. The invariant parts render first now."""
    h = _target(store)
    mh.link(id="flow", element="intake", target=h)
    # rewrite the source without 'intake' → dangling binding lint
    mh.edit(id="flow", text="flowchart TD\n  ship[Ship] --> done((Done))")
    mh.edit(id="flow", vocab="vocabulary text")
    mh.edit(id="flow", notes="notebook text")
    body = mh.get(id="flow").body
    assert body.index("## Lints") < body.index("## Mermaid source")
    assert body.index("## Mermaid source") < body.index("## Shared vocabulary")
    assert body.index("## Shared vocabulary") < body.index("## Implementation notes")
    assert body.index("## Implementation notes") < body.index("## Bindings")


def test_doc_sections_carry_their_chunk_handle(store, mh, diagram) -> None:
    """A truncated render's outline names each cut section by the handle
    that reads it whole."""
    mh.edit(id="flow", vocab="vocabulary text")
    mh.edit(id="flow", notes="notebook text")
    body = mh.get(id="flow").body
    import re

    m = re.search(r"## Implementation notes {2}\((mn\d+)\)", body)
    assert m, body
    node = mh.get(id=m.group(1)).body
    assert "notebook text" in node


def test_over_cap_render_keeps_lints_and_whole_source(
    store, mh, diagram, monkeypatch
) -> None:
    """End-to-end for the reported failure: long notes + bindings used to
    cut the source mid-flow and drop the lints entirely — a diagram with
    a dangling binding read as clean."""
    from precis._result_budget import fit_result_body

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "6000")
    h = _target(store)
    mh.link(id="flow", element="intake", target=h)
    mh.edit(id="flow", text="flowchart TD\n  ship[Ship] --> done((Done))")
    mh.edit(
        id="flow",
        notes="\n".join(f"note line {i} lorem ipsum dolor" for i in range(500)),
    )
    body = mh.get(id="flow").body
    assert len(body.encode()) > 6000
    fitted = fit_result_body(
        body,
        verb="get",
        kind="mermaid",
        context_limited=True,
        targeted_hint=None,
        read="single",
    )
    head = fitted.split("… truncated")[0]
    assert "## Lints" in head and "binding" in head
    assert "done((Done))" in head  # the LAST source line — shown whole
    assert "note line 499" not in head  # the prose takes the cut
    # the fitted outline names what dropped, handle included
    assert "Implementation notes" in fitted
    import re

    assert re.search(r"mn\d+", fitted)


def test_mn_handle_reads_the_node_through_dispatch(
    runtime_with_store, store, mh, diagram
) -> None:
    """gr793934 part 2: ``get(kind='mermaid', id='mn<N>')`` used to 404 —
    the dispatcher rewrote the node handle to ``slug~ord``, a selector
    this kind has no grammar for (same class as plan's gr495745). Bare
    and explicit-kind forms must reach DiagramHandler untouched."""
    from precis.utils import handle_registry

    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)
    out, err = runtime_with_store.dispatch_with_status("get", {"id": h})
    assert not err and "intake[Intake]" in out, out
    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "mermaid", "id": h}
    )
    assert not err and "intake[Intake]" in out, out
    # a notes chunk handle resolves the same way
    mh.edit(id="flow", notes="notebook text")
    nh = _notes_handle(store, diagram.id)
    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "mermaid", "id": nh}
    )
    assert not err and "notebook text" in out, out


def test_mn_handle_reaches_edit_through_dispatch(
    runtime_with_store, store, mh, diagram
) -> None:
    """The rewrite hit ``edit`` too — a node handle names the owning ref."""
    from precis.utils import handle_registry

    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)
    out, err = runtime_with_store.dispatch_with_status(
        "edit",
        {
            "kind": "mermaid",
            "id": h,
            "text": "flowchart TD\n  ship[Ship] --> done((Done))",
        },
    )
    assert not err and "set Mermaid source" in out, out
    assert "ship[Ship]" in mh.get(id="flow").body


def test_ref_inspection_views(store, mh, diagram) -> None:
    """Every ref-backed kind answers links / log / raw (me751839)."""
    from precis.errors import BadInput

    assert "(no links)" in mh.get(id="flow", view="links").body
    assert "# raw mermaid" in mh.get(id="flow", view="raw").body
    assert mh.get(id="flow", view="log").body.startswith("log:")
    with pytest.raises(BadInput):
        mh.get(id="flow", view="bogus")


def test_links_view_shows_the_project_edge(store, mh) -> None:
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    mh.put(id="flow2", title="F", text=_FLOW, project=proj)
    body = mh.get(id="flow2", view="links").body
    assert "Links:" in body and "proj" in body


# ── review td794121: declared args, node paging, node view refusal ──────────


def test_get_declares_its_args_keys(mh) -> None:
    """get(args=…) without an ARGS declaration left unknown keys silently
    accepted — dispatch falls back to a lenient dict (review td794121)."""
    assert "get" in type(mh).ARGS_KEYS
    assert {"rel", "of_kind", "limit", "cursor", "meta_key", "offset"} <= set(
        type(mh).ARGS_KEYS["get"]
    )


def test_get_rejects_an_undeclared_args_key(
    runtime_with_store, store, mh, diagram
) -> None:
    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "mermaid", "id": "flow", "__extras__": {"zz_bogus": 1}}
    )
    assert err and "zz_bogus" in out, out
    assert "not accepted by mermaid.get" in out, out


def test_node_handle_rejects_view_naming_the_owning_ref(store, mh, diagram) -> None:
    """view= on a node handle was silently dropped (review td794121). The
    inspection views inspect the ref — the refusal names the owning slug."""
    from precis.errors import BadInput
    from precis.utils import handle_registry

    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)
    with pytest.raises(BadInput) as exc:
        mh.get(id=h, view="links")
    nxt = str(exc.value.next)
    assert "get(kind='mermaid', id='flow'" in nxt, nxt
    assert "view='links'" in nxt, nxt


def test_node_read_pages_past_the_cap(store, mh, diagram, monkeypatch) -> None:
    """A chunk over the cap used to come back cut with no way at the rest
    (review td794121). The node read pages by ``args={'offset': N}`` — the
    rest: chain reassembles the whole source."""
    import re

    from precis._result_budget import fit_result_body
    from precis.utils import handle_registry

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "400")
    long_src = "flowchart TD\n" + "\n".join(
        f"  n{i}[node {i}] --> n{i + 1}[node {i + 1}]" for i in range(40)
    )
    mh.edit(id="flow", text=long_src)
    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)
    resp = mh.get(id=h)
    fitted = fit_result_body(
        resp.body,
        verb="get",
        kind="mermaid",
        context_limited=True,
        targeted_hint=resp.result_budget_hint,
        read="single",
        structured_fit=resp.budget_fit,
    )
    assert "… showing chars 0.." in fitted, fitted
    m = re.search(
        r"rest: get\(kind='mermaid', id='mn\d+', "
        r"args=\{'offset': (\d+)\}\)",
        fitted,
    )
    assert m, fitted
    page2 = mh.get(id=h, args={"offset": int(m.group(1))}).body
    assert f"chars {m.group(1)}.." in page2, page2
    # the chain covers the whole chunk — the tail is on a later page
    assert "n40[node 40]" in page2


def test_node_read_offset_refusals(store, mh, diagram) -> None:
    from precis.errors import BadInput
    from precis.utils import handle_registry

    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)
    total = len(mh.get(id=h).body)
    with pytest.raises(BadInput, match="at or past the end"):
        mh.get(id=h, args={"offset": total + 10})
    with pytest.raises(BadInput, match="offset"):
        mh.get(id=h, args={"offset": -1})


# ── review td794121: forward-progress paging, bounded lints, args gating ────


def test_node_page_chain_reassembles_the_chunk_through_dispatch(
    runtime_with_store, store, mh, diagram, monkeypatch
) -> None:
    """review td794121: a page whose frame alone filled the budget used to
    emit ``rest:`` back to the same offset. Follow the dispatched
    continuations to the final page — every page must advance and the
    parts must reassemble the chunk whole."""
    import re

    from precis.utils import handle_registry

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "400")
    long_src = "flowchart TD\n" + "\n".join(
        f"  n{i}[node {i}] --> n{i + 1}[node {i + 1}]" for i in range(40)
    )
    mh.edit(id="flow", text=long_src)
    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)

    parts: list[str] = []
    payload: dict = {"kind": "mermaid", "id": h}
    prev_end: int | None = None
    for _ in range(60):
        out, err = runtime_with_store.dispatch_with_status("get", payload)
        assert not err, out
        m = re.search(r"\(chars (\d+)\.\.(\d+) of (\d+)\)\n\n(.*)", out, re.S)
        assert m, out
        start, end, total = int(m[1]), int(m[2]), int(m[3])
        assert end > start  # forward progress on every page
        assert prev_end is None or start == prev_end
        assert len(out.encode("utf-8")) <= 400, out
        prev_end = end
        rest = m[4]
        part, sep, _tail = rest.partition("\n\n… showing")
        parts.append(part)
        if not sep:
            assert end == total
            break
        nxt = re.search(r"args=\{'offset': (\d+)\}", rest)
        assert nxt and int(nxt[1]) == end, out
        payload = {"kind": "mermaid", "id": h, "__extras__": {"offset": end}}
    else:
        pytest.fail("the rest: chain did not finish")
    assert "".join(parts) == long_src


def test_node_page_refuses_when_the_frame_alone_wont_fit(
    runtime_with_store, store, mh, diagram, monkeypatch
) -> None:
    """A budget below the header + ``rest:`` footer used to either
    overrun the cap or emit a self-restarting continuation. Now the
    typed refusal names the same read — the cap is what must move."""
    from precis.utils import handle_registry

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "80")
    node = _source_chunk_id(store, diagram.id)
    h = handle_registry.format_handle("mermaid", node, chunk=True)
    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "mermaid", "id": h}
    )
    assert err, out
    assert "cannot fit even one character" in out, out
    assert f"id='{h}'" in out, out


def test_lint_list_is_bounded_and_pages_through_lints_view(
    store, mh, diagram, monkeypatch
) -> None:
    """review td794121: an unbounded lint list ahead of the source could
    still crowd it out. The slug render shows a bounded preview naming
    view='lints'; the view itself pages to the end by args={'offset': N}."""
    import re

    from precis._result_budget import fit_result_body

    h = _target(store)
    for i in range(30):
        mh.link(id="flow", element=f"ghost{i}", target=h)
    body = mh.get(id="flow").body
    shown = re.findall(r"^- \[binding\]", body, re.M)
    assert len(shown) == 20, body
    assert "… and 10 more — get(kind='mermaid', id='flow', view='lints')" in body

    whole = mh.get(id="flow", view="lints").body
    assert len(re.findall(r"^- \[binding\]", whole, re.M)) == 30, whole

    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "400")
    resp = mh.get(id="flow", view="lints")
    fitted = fit_result_body(
        resp.body,
        verb="get",
        kind="mermaid",
        context_limited=True,
        targeted_hint=resp.result_budget_hint,
        read="single",
        structured_fit=resp.budget_fit,
    )
    assert "… showing chars 0.." in fitted, fitted
    m = re.search(r"args=\{'offset': (\d+)\}", fitted)
    assert m, fitted
    page2 = mh.get(id="flow", view="lints", args={"offset": int(m[1])}).body
    assert f"chars {m[1]}.." in page2
    assert "ghost29" in page2  # the last finding lands on a later page


def test_view_args_meant_for_another_view_refuse(mh, diagram) -> None:
    """review td794121: offset on links, limit on raw and offset on log
    used to be silently dropped. Each refusal names the door that reads
    the key."""
    from precis.errors import BadInput

    with pytest.raises(BadInput) as exc:
        mh.get(id="flow", view="links", args={"offset": 3})
    assert "offset" in str(exc.value) and "lints" in str(exc.value)

    with pytest.raises(BadInput) as exc:
        mh.get(id="flow", view="raw", args={"limit": 2})
    assert "limit" in str(exc.value) and "view='links'" in str(exc.value)

    with pytest.raises(BadInput) as exc:
        mh.get(id="flow", view="log", args={"offset": 1})
    assert "offset" in str(exc.value)

    # the keys each view does take still pass
    assert mh.get(id="flow", view="log", args={"limit": 5}).body.startswith("log:")
    assert mh.get(id="flow", view="raw", args={"meta_key": "render"}).body


def test_list_path_refuses_args(mh, diagram) -> None:
    """review td794121: declared args on the no-id list path silently
    did nothing."""
    from precis.errors import BadInput

    with pytest.raises(BadInput) as exc:
        mh.get(args={"offset": 1})
    assert "list takes no args" in str(exc.value), str(exc.value)
    nxt = str(exc.value.next)
    assert "get(kind='mermaid', id='<slug>'" in nxt, nxt


def test_log_view_args_dispatch(runtime_with_store, store, mh, diagram) -> None:
    """log's own keys ride the declared-args channel too."""
    out, err = runtime_with_store.dispatch_with_status(
        "get",
        {
            "kind": "mermaid",
            "id": "flow",
            "view": "log",
            "__extras__": {"limit": 5},
        },
    )
    assert not err and out.startswith("log:"), out
    out, err = runtime_with_store.dispatch_with_status(
        "get",
        {
            "kind": "mermaid",
            "id": "flow",
            "view": "links",
            "__extras__": {"offset": 3},
        },
    )
    assert err and "don't apply to mermaid view='links'" in out, out
