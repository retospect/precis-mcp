"""Smartdraft reader route (``GET /smartdraft/{ident}``) — the fisheye-rail
three-pane HTML surface (design: docs/proposals/draft-reader-fisheye-rail.md).

The classic ``/drafts`` fixture (``tests/precis_web/test_drafts.py``'s
``DraftFakeStore``) predates the smartdraft reader: its chunk stand-in lacks
``.dc`` (the deterministic-chunk-address ``ChunkNode`` needs), so it 500s
smartdraft's ``_build_nodes_uncached`` (gripe 171217). Rather than risk that
large shared fixture, this file carries its own small ``FakeStore`` subclass
whose chunk stand-in supplies exactly the fields ``_build_nodes_uncached``
reads (``smartdraft.py`` ~lines 218-250): ``dc``, ``handle``, ``chunk_id``,
``depth``, ``chunk_kind``, ``text``, ``meta`` (``getattr``-guarded — only a
``chunk_kind='table'`` chunk needs it, for ``table_payload``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.utils import handle_registry
from precis_web import smartdraft
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime, FakeStore, make_ref

_DRAFT = make_ref(id=700, kind="draft", slug="sdt", title="Smartdraft reader draft")


def _sd_chunk(
    chunk_id: int,
    kind: str,
    text: str,
    depth: int,
    *,
    meta: dict[str, object] | None = None,
) -> SimpleNamespace:
    handle = f"H{chunk_id:06d}"
    return SimpleNamespace(
        handle=handle,
        dc=handle_registry.format_handle("draft", chunk_id, chunk=True),
        chunk_kind=kind,
        text=text,
        depth=depth,
        chunk_id=chunk_id,
        ref_id=700,
        meta=meta or {},
    )


class SmartDraftFakeStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self._chunks = [
            _sd_chunk(1, "heading", "Smartdraft reader draft", 0),
            _sd_chunk(
                2,
                "paragraph",
                "First body paragraph about alpha topics, see paper:acheson26.",
                1,
            ),
            _sd_chunk(3, "paragraph", "Second body paragraph about beta topics.", 1),
            # A table chunk (ADR 0035 §1) — exercises the shared tableEditor
            # grid (gripe 56746) in the smartdraft focus pane.
            _sd_chunk(
                4,
                "table",
                "| A | B |\n| --- | --- |\n| 1 | 2 |",
                1,
                meta={"table": {"header": ["A", "B"], "rows": [["1", "2"]]}},
            ),
            # A blob-backed figure (ADR 0034/0058) — exercises the shared
            # figure media render + clearance badge (gripe 56668) in the
            # smartdraft focus pane + the Collaborate pane's clearance list.
            _sd_chunk(
                5,
                "figure",
                "Fig 1. A diagram.",
                0,
                meta={"figure": {"origin": "original"}},
            ),
            # A registry term (ADR 0052) carrying a dedicated ``abbrev``
            # (gripe 56690) — exercises ChunkNode.is_term / term_abbrev and
            # the Collaborate-pane "occurs in N places" backlink rail.
            _sd_chunk(
                6,
                "term",
                "stereolithography",
                1,
                meta={
                    "registry": "glossary",
                    "short": "stereolithography",
                    "abbrev": "STL",
                },
            ),
            # Uses the LONG form only.
            _sd_chunk(
                7,
                "paragraph",
                "The prototype was printed via stereolithography overnight.",
                1,
            ),
            # Uses ONLY the dedicated abbrev surface — proves it resolves
            # independently of the short/long form for occurrence matching.
            _sd_chunk(8, "paragraph", "STL parts cure under UV light.", 1),
            # Mentions neither surface — must NOT show up as an occurrence.
            _sd_chunk(9, "paragraph", "Unrelated paragraph about topology.", 1),
        ]

    def get_ref(self, *, kind, id):
        if kind == "draft" and id in ("sdt", 700):
            return _DRAFT
        return super().get_ref(kind=kind, id=id)

    def list_refs(self, *, kind=None, limit=50, offset=0, **kw):
        if kind == "draft":
            return [_DRAFT]
        return super().list_refs(kind=kind, limit=limit, offset=offset, **kw)

    def reading_order(self, ref_id):
        return list(self._chunks)

    def block_views(self, ref_id, handles=None):
        # keyed by handle (matches _build_nodes_uncached's views.get(c.handle))
        return {
            "H000002": {"summary": "Alpha gist.", "keywords": "alpha"},
            "H000003": {"summary": "Beta gist.", "keywords": "beta"},
        }

    def fetch_refs_by_ids(self, ids, *, include_deleted=False):
        # draft_eyes.load_marks reads ref.meta off this — base FakeStore's
        # per-kind pools don't carry a "draft" bucket, so splice it in.
        base = super().fetch_refs_by_ids(ids, include_deleted=include_deleted)
        if 700 in ids:
            base[700] = _DRAFT
        return base

    def get_chunk_blob(self, handle):
        if handle == "H000005":
            return (b"\x89PNG\r\n\x1a\n", "image/png")
        return None

    def has_chunk_blob(self, chunk_id) -> bool:
        # The fixture figure (chunk_id=5) is a real blob-backed image (ADR
        # 0058 medium resolver) — mirrors DraftFakeStore's FIGFIG.
        return chunk_id == 5


@pytest.fixture
def smartdraft_runtime() -> FakeRuntime:
    return FakeRuntime(SmartDraftFakeStore())


@pytest.fixture
def smartdraft_client(smartdraft_runtime: FakeRuntime, tmp_path) -> TestClient:
    app = create_app(
        runtime=smartdraft_runtime, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def test_smartdraft_reader_renders_three_panes(
    smartdraft_client: TestClient,
) -> None:
    """The reader 200s and mounts all three panes: left fisheye TOC (rows
    carry ``data-dc``), middle focus (``#mid-focus``), right collaborate
    (the "Collaborate" pane header)."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert 'id="sd-content"' in body  # the 3-pane grid mounted at all
    assert 'data-dc="' in body  # left pane: TOC rows keyed by dc
    assert 'id="mid-focus"' in body  # middle pane: the rendered focus chunk
    assert "Collaborate" in body  # right pane header


def test_smartdraft_reader_uses_shared_draft_edit(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56746: the focus para's inline editor is the SHARED
    ``draft_editors.draft_edit`` macro (drafts/_editors.html.j2) — the same
    ProseMirror rich editor + `[`-citation autocomplete the classic /drafts
    reader uses — not smartdraft's old plain-``smartEdit`` textarea."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    # the shared component's mount markers
    assert "window.__mountDraftPM" in body  # ProseMirror bootstrap loaded
    assert "draftEdit(" in body  # the shared Alpine component, instantiated
    assert 'x-ref="pm"' in body  # the ProseMirror mount point
    assert "pm-ac" in body  # the `[` citation-autocomplete dropdown CSS
    # the retired smartdraft-only plain-textarea editor is gone
    assert "smartEdit(" not in body


def test_smartdraft_reader_table_focus_uses_shared_table_editor(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56746: focusing a ``chunk_kind='table'`` chunk renders the
    SHARED ``draft_editors.draft_table_editor`` grid (⊞ edit table) — the
    same structured editor the classic /drafts reader uses — instead of the
    raw pipe-markdown text smartdraft rendered before tables had their own
    editor here."""
    table_dc = handle_registry.format_handle("draft", 4, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={table_dc}")
    assert r.status_code == 200
    body = r.text
    assert "tableEditor(" in body  # the shared Alpine component, instantiated
    assert "⊞ edit table" in body
    # the recovered table renders as a real <table>, not raw pipe-markdown
    assert "<table" in body
    assert "<th" in body and ">A<" in body and ">B<" in body


def test_needs_items_reads_dict_keys_not_attributes(monkeypatch) -> None:
    """Regression: ``_needs_items`` walks ``_work_items``' dict rows. The old
    code read them with ``getattr(w, "todo_id", None)`` — attribute access on
    a dict always misses, so every row silently defaulted to
    ``todo_id=None``/``title=""``/``status="open"`` and the "Needs · in-flight"
    pane rendered a blank row linking to ``/r/todo/None``. This pins the fix:
    real dict values must survive the walk, including the last job's status
    and the blocked/no-jobs fallback."""
    from precis_web.routes import drafts as drafts_mod
    from precis_web.routes.smartdraft import _needs_items

    work_items = [
        {
            "todo_id": 4242,
            "title": "Fix the intro paragraph",
            "blocked": True,
            "jobs": [],
            "asks": [{"tag": "clarify", "question": "Which section?"}],
            "ask_tags": ["clarify"],
        },
        {
            "todo_id": 4343,
            "title": "Rewrite the conclusion",
            "blocked": False,
            "jobs": [
                {"id": 1, "status": "done", "reason": None},
                {"id": 2, "status": "running", "reason": None},
            ],
            "asks": [],
            "ask_tags": [],
        },
    ]
    monkeypatch.setattr(drafts_mod, "_work_items", lambda store, ref_id: work_items)

    rows = _needs_items(store=None, ref_id=700)

    assert len(rows) == 2
    blocked_row, running_row = rows

    # The real todo_id must survive — old getattr-based code always yielded
    # None here (attribute access on a dict never finds "todo_id").
    assert blocked_row["todo_id"] == 4242
    assert blocked_row["title"] == "Fix the intro paragraph"
    assert blocked_row["blocked"] is True
    # No jobs + blocked=True -> "blocked" fallback, not the old "open" default.
    assert blocked_row["status"] == "blocked"
    assert blocked_row["asks"] == ["Which section?"]

    assert running_row["todo_id"] == 4343
    assert running_row["title"] == "Rewrite the conclusion"
    assert running_row["blocked"] is False
    # Status comes from the LAST job, not the first or a hardcoded default.
    assert running_row["status"] == "running"
    assert running_row["asks"] == []


def test_smartdraft_reader_figure_focus_renders_image_and_clearance_badge(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56668: focusing a ``chunk_kind='figure'`` chunk renders the
    SHARED ``draft_figures.figure_media`` image + ``clearance_badge`` (the
    same markup the classic /drafts reader uses) — not the raw caption text
    smartdraft rendered before figures had their own render here."""
    fig_dc = handle_registry.format_handle("draft", 5, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={fig_dc}")
    assert r.status_code == 200
    body = r.text
    # the actual <img> pointed at the blob route (not raw caption text as a <p>)
    assert '<img src="/drafts/blob/H000005"' in body
    # origin chip + clearance badge (cleared — an "original" blob-backed figure)
    assert ">original<" in body
    assert "✓ cleared" in body


def test_smartdraft_collaborate_pane_has_figure_upload_and_clearance_list(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56668: the right ("Collaborate") pane exposes the figure-upload
    control (posts to the SAME ``/drafts/{ident}/figure`` endpoint the
    classic reader uses) and a clearance-surfacing list of the draft's
    figures — regardless of which chunk is currently focused."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    # the upload control (shared draft_figures.figure_upload_form)
    assert 'action="/drafts/sdt/figure"' in body
    assert 'name="file"' in body and 'accept="image/*"' in body
    # the clearance-surfacing list (shared draft_figures.clearance_badge,
    # keyed to the fixture figure's own handle — not the current focus)
    assert 'action="/drafts/sdt/figure/H000005/permission"' in body
    assert "Figures · clearance" in body


def test_smartdraft_reader_404s_on_unknown_draft(
    smartdraft_client: TestClient,
) -> None:
    r = smartdraft_client.get("/smartdraft/does-not-exist")
    assert r.status_code == 404


def test_smartdraft_reader_term_abbrev_is_a_resolvable_occurrence_surface(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56690: a term's dedicated ``abbrev`` resolves occurrences on its
    own — a paragraph mentioning *only* the abbreviation ("STL parts cure
    under UV light.") still shows up in the "occurs in N places" rail when
    the term is focused, independent of the long-form ``short`` surface."""
    term_dc = handle_registry.format_handle("draft", 6, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={term_dc}")
    assert r.status_code == 200
    body = r.text
    long_dc = handle_registry.format_handle("draft", 7, chunk=True)
    abbrev_dc = handle_registry.format_handle("draft", 8, chunk=True)
    unrelated_dc = handle_registry.format_handle("draft", 9, chunk=True)
    # scope to the Collaborate-pane occurrences rail — data-dc also appears
    # (unrelated to this feature) on every left-pane TOC row.
    rail = body[body.index("Occurs in") : body.index("<textarea")]
    assert f'data-dc="{long_dc}"' in rail  # long-form usage
    assert f'data-dc="{abbrev_dc}"' in rail  # abbrev-only usage
    assert f'data-dc="{unrelated_dc}"' not in rail  # no mention, not listed


def test_smartdraft_reader_term_focus_lists_occurrences_as_focus_nav_links(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56690: focusing a ``chunk_kind='term'`` chunk renders its
    occurrences (computed from the already-loaded node set) as smartdraft
    focus-nav links (``?focus=dc<id>`` / ``data-dc``) with a count."""
    term_dc = handle_registry.format_handle("draft", 6, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={term_dc}")
    assert r.status_code == 200
    body = r.text
    assert "Occurs in 2 places" in body
    long_dc = handle_registry.format_handle("draft", 7, chunk=True)
    assert f"?focus={long_dc}" in body  # a real focus-nav link, not a dead row


def test_smartdraft_reader_non_term_focus_has_no_occurrences_rail(
    smartdraft_client: TestClient,
) -> None:
    """The occurrences rail only renders for an ``is_term`` focus — a plain
    paragraph focus shows no "Occurs in" section."""
    r = smartdraft_client.get("/smartdraft/sdt")  # default focus: first body para
    assert r.status_code == 200
    assert "Occurs in" not in r.text


def _term_node(
    idx: int, *, short: str, abbrev: str | None, text: str
) -> smartdraft.ChunkNode:
    return smartdraft.ChunkNode(
        idx=idx,
        dc=handle_registry.format_handle("draft", 100 + idx, chunk=True),
        base58=f"term{idx}",
        chunk_id=100 + idx,
        depth=1,
        chunk_kind="term",
        text=text,
        summary="",
        keywords=[],
        term_short=short,
        term_abbrev=abbrev,
    )


def _para_node(idx: int, text: str) -> smartdraft.ChunkNode:
    return smartdraft.ChunkNode(
        idx=idx,
        dc=handle_registry.format_handle("draft", 100 + idx, chunk=True),
        base58=f"para{idx}",
        chunk_id=100 + idx,
        depth=1,
        chunk_kind="paragraph",
        text=text,
        summary="",
        keywords=[],
    )


def test_term_occurrences_excludes_definition_prose_and_case_variants() -> None:
    """gripe: ``term_occurrences`` used to also match the term's own
    ``text`` (its DEFINITION, not a lookup surface) and matched
    case-insensitively — both diverge from what
    :func:`precis_web.linkify._highlight_abbrevs` actually highlights, so
    the "occurs in N places" count listed paragraphs with no live
    ``<abbr class="pa">`` highlight. A REALISTIC term (``short`` distinct
    from a genuine prose ``text`` definition, plus an ``abbrev``) should
    count only the ``short``/``abbrev`` usages — not the definition-prose
    mention, and not a differently-cased mention of ``short``."""
    term = _term_node(
        0,
        short="stereolithography",
        abbrev="STL",
        text="a common 3D-printing process",
    )
    nodes = [
        term,
        _para_node(1, "The part was made via stereolithography overnight."),
        _para_node(2, "STL parts cure under UV light."),
        _para_node(3, "This technique is a common 3D-printing process used widely."),
        _para_node(4, "Stereolithography (capitalized) starts a sentence."),
    ]
    occ = smartdraft.term_occurrences(nodes, term)
    assert [n.idx for n in occ] == [1, 2]  # short usage + abbrev usage only
    assert len(occ) == 2
    # neither the definition-prose paragraph nor the case-variant qualifies
    assert 3 not in [n.idx for n in occ]
    assert 4 not in [n.idx for n in occ]


def test_term_surfaces_excludes_definition_text() -> None:
    """``_term_surfaces`` mirrors ``defined_terms``' surface set exactly —
    ``term_short``/``term_abbrev``/``term_surface_forms``, never ``text``
    (the definition prose)."""
    term = _term_node(
        0, short="stereolithography", abbrev="STL", text="a definition, not a surface"
    )
    surfaces = smartdraft._term_surfaces(term)
    assert set(surfaces) == {"stereolithography", "STL"}
    assert "a definition, not a surface" not in surfaces


def test_smartdraft_cited_sources_panel_lists_focus_block_paper_citations(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56635: the Collaborate rail's "Cited sources" panel lists the
    FOCUS block's paper citations as new-tab links (opening the paper
    reader in a new tab so the writing surface stays put) — carrying no
    ``data-dc`` (so the no-reload nav interceptor leaves them alone)."""
    # Default focus is chunk 2, "...see paper:acheson26." (the first body
    # paragraph — see SmartDraftFakeStore._chunks).
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert "Cited sources" in body
    panel = body[body.index("Cited sources") : body.index("Pinned:")]
    assert 'href="/r/paper/acheson26"' in panel
    assert 'target="_blank"' in panel
    assert "data-dc=" not in panel


def test_smartdraft_cited_sources_panel_omitted_when_focus_has_no_cites(
    smartdraft_client: TestClient,
) -> None:
    """A focus block that cites no paper shows no "Cited sources" panel at
    all (mirrors how "Occurs in N places" omits when empty)."""
    no_cite_dc = handle_registry.format_handle("draft", 3, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={no_cite_dc}")
    assert r.status_code == 200
    assert "Cited sources" not in r.text


def test_smartdraft_reader_popover_is_teleported_to_body(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56806: a hover-preview card rendered inside smartdraft's
    overflow-clipped panes must be wrapped in ``<template x-teleport=
    "body">`` (the shared ``linkify._anchor_html`` fix) so it escapes the
    clip — and the page's now-obsolete per-pane "portal-lite" JS mitigation
    (fixed-coords-on-open listener keyed off ``ref-popover-open``, plus the
    ``data-sd-portaled`` reaping hack) must be gone."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert 'href="/r/paper/acheson26"' in body  # the ref actually linkified
    assert '<template x-teleport="body">' in body
    assert "ref-popover" in body
    # the card-level pointer-bridge handler (gripe 56806 regression #1)
    assert '@mouseenter="clearTimeout(closeTimer); hovered = true"' in body
    # obsolete smartdraft-only mitigations, superseded by the shared fix
    assert "data-sd-portaled" not in body
    assert "pop.style.position = 'fixed'" not in body
