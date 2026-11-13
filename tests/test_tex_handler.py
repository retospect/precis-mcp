"""Tests for TexHandler — confirms the thin subclass of PlaintextHandler
correctly routes to ``kind='tex'`` and uses ``.tex`` extension.

The shared paragraph-block / edit / put / search / link / tag pipeline
is exercised by ``test_plaintext_handler.py``; we don't re-test it here.
This file only covers the differences introduced by the subclass:

- ``kind='tex'`` in store rows + KindSpec
- ``.tex`` extension in walker, file writes, slug resolution
- error messages mention ``tex`` (not ``plaintext``) when called
  through the subclass
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, Gone, NotFound, Upstream
from precis.handlers.plaintext import PlaintextHandler
from precis.handlers.tex import TexHandler
from precis.store import Store
from tests.hintcheck import assert_hints_round_trip


@pytest.fixture
def tex_root(tmp_path: Path) -> Path:
    """Handler root is a subdirectory of ``tmp_path`` so the path-
    safety tests can drop files at ``tmp_path / 'outside'`` and prove
    they're invisible / unreachable."""
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def handler(hub: Hub, tex_root: Path) -> TexHandler:
    return TexHandler(hub=hub, root=tex_root)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── construction ─────────────────────────────────────────────────────


def test_construction_fails_on_missing_root(store: Store, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tex root"):
        TexHandler(hub=Hub(store=store), root=tmp_path / "no-such-dir")


def test_construction_fails_on_file_root(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "f.tex"
    f.write_text(r"\section{intro}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        TexHandler(hub=Hub(store=store), root=f)


# ── classvars match the subclass intent ──────────────────────────────


def test_kind_classvars_overridden() -> None:
    assert TexHandler._KIND == "tex"
    assert TexHandler._EXTENSIONS == (".tex",)
    assert TexHandler._DEFAULT_EXT == ".tex"
    # Sanity: parent class still says plaintext.
    assert PlaintextHandler._KIND == "plaintext"
    assert PlaintextHandler._EXTENSIONS == (".txt", ".log", ".bib")


def test_spec_advertises_tex_kind() -> None:
    assert TexHandler.spec.kind == "tex"
    assert TexHandler.spec.title == "LaTeX"


# ── walker / index respects extension ────────────────────────────────


def test_index_lists_tex_files_only(handler: TexHandler, tex_root: Path) -> None:
    _write(tex_root, "intro.tex", r"\section{Introduction}")
    _write(tex_root, "chapters/methods.tex", r"\section{Methods}")
    # Non-tex extensions must NOT show up.
    _write(tex_root, "notes.txt", "plaintext sibling")
    _write(tex_root, "readme.md", "# md sibling")
    out = handler.get()
    assert "2 tex file(s)" in out.body
    assert "intro" in out.body
    assert "chapters--methods" in out.body
    assert "notes" not in out.body
    assert "readme" not in out.body


def test_empty_root_lists_no_tex_files(handler: TexHandler) -> None:
    out = handler.get()
    assert "no tex files" in out.body


# ── overview + block reads route via kind='tex' ──────────────────────


def test_overview_renders(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "intro.tex",
        r"\section{Introduction}" + "\n\n" + "First paragraph body.\n",
    )
    out = handler.get(id="intro")
    assert "intro" in out.body
    assert "paragraphs:" in out.body


def test_overview_for_missing_file_raises(handler: TexHandler) -> None:
    with pytest.raises(NotFound, match="tex file"):
        handler.get(id="nonexistent")


# ── gr661900: handle-shaped / kind-prefixed ids name the right door ──


def test_chunk_handle_id_hints_at_direct_get(handler: TexHandler) -> None:
    """``xc<N>`` pasted into ``id=`` is a tex chunk handle, not a file
    slug — the error names what the id is and that ``get(id=…)`` with
    no ``kind=`` resolves it through dispatch."""
    with pytest.raises(BadInput) as exc:
        handler.get(id="xc1131007")
    msg = str(exc.value)
    assert "tex chunk handle" in msg
    assert "not a tex file slug" in msg
    assert "get(id='xc1131007')" in msg


def test_foreign_record_handle_id_hints_at_direct_get(
    handler: TexHandler,
) -> None:
    """A handle of another kind names that kind and the same
    self-identifying ``get(id=…)`` read."""
    with pytest.raises(BadInput) as exc:
        handler.get(id="pa123")
    msg = str(exc.value)
    assert "paper handle" in msg
    assert "not a tex file slug" in msg
    assert "get(id='pa123')" in msg


def test_no_read_chunk_handle_names_the_owning_ref(
    handler: TexHandler,
) -> None:
    """``gc<N>`` (gripe chunk) has no direct handle read — the hint
    names the owning-ref door, not ``get(id=…)``, which would fail a
    second time."""
    with pytest.raises(BadInput) as exc:
        handler.get(id="gc42")
    msg = str(exc.value)
    assert "gripe chunk handle" in msg
    assert "get(kind='gripe'" in msg
    assert "get(id='gc42')" not in msg


def test_same_kind_record_handle_resolves(handler: TexHandler, tex_root: Path) -> None:
    """A ``tx<N>`` record handle reaching the handler directly
    resolves by ref_id, mirroring ``resolve_live_slug_ref`` — the
    same call dispatch rewrites upstream."""
    _write(tex_root, "intro.tex", "\\section{Introduction}\n\nBody text.\n")
    handler.get(id="intro")
    ref = handler.store.get_ref(kind="tex", id="intro")
    assert ref is not None
    out = handler.get(id=f"tx{ref.id}")
    assert "intro" in out.body


def test_kind_prefixed_slug_explains_the_dir(
    handler: TexHandler, tex_root: Path
) -> None:
    """``tex--outlook`` is the double-prefix guess: '--' encodes '/',
    so it addresses ``tex/outlook.tex`` while the root file's slug is
    ``outlook`` — the hint says so and leads suggestions with it."""
    _write(tex_root, "outlook.tex", "\\section{Outlook}\n\nBody text.\n")
    with pytest.raises(NotFound) as exc:
        handler.get(id="tex--outlook")
    msg = str(exc.value)
    assert "tex file 'tex--outlook' not found" in msg
    assert "tex/outlook.tex" in msg
    assert "'outlook'" in msg
    assert exc.value.options is not None
    assert exc.value.options[0] == "outlook"


def test_kind_prefix_hint_when_bare_slug_absent(handler: TexHandler) -> None:
    """Even with no ``outlook.tex`` on disk the '--' mapping is
    explained — the generic fuzzy suggestions still apply."""
    with pytest.raises(NotFound) as exc:
        handler.get(id="tex--outlook")
    assert "tex/outlook.tex" in str(exc.value)


def test_path_form_kind_prefixed_explains(handler: TexHandler) -> None:
    """``tex/background.tex`` canonicalizes to ``tex--background`` —
    the same dir-mapping explanation (gr661900 arg sample)."""
    with pytest.raises(NotFound) as exc:
        handler.get(id="tex/background.tex")
    msg = str(exc.value)
    assert "tex--background" in msg
    assert "tex/background.tex" in msg


def test_search_scope_chunk_handle_hints(handler: TexHandler) -> None:
    """``search(scope=)`` misses route through the same handle-aware
    error as ``get(id=)``."""
    with pytest.raises(BadInput) as exc:
        handler.search(q="anything", scope="xc1131007")
    assert "get(id='xc1131007')" in str(exc.value)


def test_search_scope_record_handle_resolves(
    handler: TexHandler, tex_root: Path
) -> None:
    _write(tex_root, "intro.tex", "\\section{Introduction}\n\nmarker text.\n")
    handler.get(id="intro")
    ref = handler.store.get_ref(kind="tex", id="intro")
    assert ref is not None
    out = handler.search(q="marker", scope=f"tx{ref.id}")
    assert "block hit" in out.body
    assert "Introduction" in out.body


def test_off_disk_tex_ref_serves_stored_blocks(
    handler: TexHandler, tex_root: Path
) -> None:
    """gr659000: a tex ref whose file isn't under this root (ingested
    on another host, or deleted here) still reads — overview + /raw
    serve the stored blocks — and the read must not retire the ref.
    """
    p = _write(
        tex_root,
        "intro.tex",
        "\\section{Introduction}\n\nFirst paragraph body.\n",
    )
    handler.get(id="intro")
    p.unlink()
    out = handler.get(id="intro")
    assert "on disk:" in out.body
    assert handler.store.get_ref(kind="tex", id="intro") is not None
    raw = handler.get(id="intro/raw")
    assert "reconstructed" in raw.body
    assert "First paragraph body." in raw.body


def test_get_block_by_pos(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "doc.tex",
        r"\section{One}" + "\n\n" + r"\section{Two}" + "\n",
    )
    handler.get(id="doc")  # force ingest
    out = handler.get(id="doc~0")
    assert "One" in out.body
    out = handler.get(id="doc~1")
    assert "Two" in out.body


# ── put / edit / delete go through the same pipeline ─────────────────


def test_put_create_writes_tex_file(handler: TexHandler, tex_root: Path) -> None:
    out = handler.put(
        id="new-paper",
        text=r"\section{Hello}" + "\n\nBody paragraph.\n",
        mode="create",
    )
    assert "created tex" in out.body
    # Lands as .tex on disk, not .txt.
    assert (tex_root / "new-paper.tex").exists()
    assert not (tex_root / "new-paper.txt").exists()


def test_put_create_refuses_overwrite(handler: TexHandler, tex_root: Path) -> None:
    _write(tex_root, "exists.tex", r"\section{x}")
    with pytest.raises(BadInput, match="file already exists"):
        handler.put(id="exists", text="new", mode="create")


def test_edit_find_replace(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "paper.tex",
        r"\section{Old Title}" + "\n\nBody paragraph.\n",
    )
    out = handler.edit(
        id="paper",
        mode="find-replace",
        find="Old Title",
        text="New Title",
    )
    # Unified write-result shape (MCP critic MAJOR-C 2026-05-02).
    assert out.body.startswith("edited chunk ")
    assert "'paper'" in out.body
    assert " (L" in out.body
    content = (tex_root / "paper.tex").read_text(encoding="utf-8")
    assert "New Title" in content
    assert "Old Title" not in content


def test_edit_append(handler: TexHandler, tex_root: Path) -> None:
    _write(tex_root, "doc.tex", r"\section{One}" + "\n")
    out = handler.edit(
        id="doc",
        mode="append",
        text=r"\section{Two}",
    )
    assert out.body.startswith("appended chunk ")
    assert "'doc'" in out.body
    assert r"\section{Two}" in (tex_root / "doc.tex").read_text(encoding="utf-8")


def test_search_routes_via_tex_kind(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "paper.tex",
        r"\section{NOxRR mechanism}" + "\n\nDetailed analysis here.\n",
    )
    handler.get(id="paper")  # force ingest
    out = handler.search(q="mechanism")
    # The match must surface at all (kind routing works).
    assert "paper" in out.body or "mechanism" in out.body.lower()


def test_search_empty_corpus(handler: TexHandler) -> None:
    out = handler.search(q="anything")
    assert "no tex blocks match" in out.body


# ── slug + extension boundary ────────────────────────────────────────


def test_invalid_slug_rejected(handler: TexHandler) -> None:
    with pytest.raises(BadInput, match="path traversal"):
        handler.get(id="../escape")


def test_txt_extension_not_picked_up(handler: TexHandler, tex_root: Path) -> None:
    """A bare ``.txt`` file in the tex root must be invisible to TexHandler.

    Confirms the extension probe is scoped to ``_EXTENSIONS=('.tex',)``.
    """
    _write(tex_root, "loose.txt", "this is plaintext, not tex")
    with pytest.raises(NotFound, match="tex file"):
        handler.get(id="loose")


# ── section-aware ingest (Phase B end-to-end) ────────────────────────


def test_section_blocks_stored_with_metadata(
    handler: TexHandler, tex_root: Path
) -> None:
    """A heading ingests as its own block; the meta JSON records
    section_level / section_title / section_path."""
    _write(
        tex_root,
        "paper.tex",
        r"\section{Methods}"
        + "\n\n"
        + r"\subsection{Kinetics}"
        + "\n\n"
        + "We measured kcat across pH 4 to 9.\n",
    )
    handler.get(id="paper")  # force ingest
    ref = handler.store.get_ref(kind="tex", id="paper")
    assert ref is not None
    blocks = handler.store.chunks.list_chunks_for_ref(ref.id)
    # Find each by content.
    methods = [b for b in blocks if b.text == r"\section{Methods}"][0]
    kinetics = [b for b in blocks if b.text == r"\subsection{Kinetics}"][0]
    body = [b for b in blocks if b.text.startswith("We measured")][0]

    assert methods.meta["section_level"] == 0
    assert methods.meta["section_title"] == "Methods"

    assert kinetics.meta["section_level"] == 1
    assert kinetics.meta["section_title"] == "Kinetics"
    # Subsection records its parent section. The
    # ``meta['section_path']`` key is consumed at write time by
    # ``Store.insert_chunks`` — popped into the dedicated
    # ``chunks.section_path`` TEXT[] column. On read, the row
    # mapper surfaces the column back into the meta dict (so
    # consumers like the oracle entry-title resolver don't have
    # to learn the split). The (level, title) pairs ride alongside
    # in ``section_path_pairs`` for any consumer that needs the
    # nesting.
    assert kinetics.meta["section_path"] == ["Methods"]
    assert kinetics.meta["section_path_pairs"] == [[0, "Methods"]]

    # The body paragraph isn't a section heading itself but knows
    # which section ancestry it sits inside.
    assert "section_level" not in body.meta
    assert body.meta["section_path"] == ["Methods", "Kinetics"]
    assert body.meta["section_path_pairs"] == [[0, "Methods"], [1, "Kinetics"]]


def test_inputs_stored_in_block_meta(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "main.tex",
        r"\section{Top}" + "\n\n" + r"\input{chapters/intro}" + "\n",
    )
    handler.get(id="main")
    ref = handler.store.get_ref(kind="tex", id="main")
    assert ref is not None
    blocks = handler.store.chunks.list_chunks_for_ref(ref.id)
    input_block = [b for b in blocks if "\\input" in b.text][0]
    assert input_block.meta["inputs"] == ["chapters/intro"]


# ── /toc view (Phase C) ──────────────────────────────────────────────


def test_toc_view_renders_section_tree(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "paper.tex",
        r"\section{Introduction}"
        + "\n\n"
        + "Body of intro.\n\n"
        + r"\section{Methods}"
        + "\n\n"
        + r"\subsection{Materials}"
        + "\n\n"
        + r"\subsection{Procedure}"
        + "\n\n"
        + r"\section{Results}"
        + "\n",
    )
    out = handler.get(id="paper", view="toc")
    body = out.body
    assert "TOC: paper" in body
    assert r"\section{Introduction}" in body
    assert r"\section{Methods}" in body
    assert r"\subsection{Materials}" in body
    assert r"\subsection{Procedure}" in body
    assert r"\section{Results}" in body
    # Subsections rendered indented relative to their parent section.
    intro_idx = body.index(r"\section{Introduction}")
    materials_idx = body.index(r"\subsection{Materials}")
    # Materials line begins with more leading whitespace than Introduction.
    intro_line = body[body.rfind("\n", 0, intro_idx) + 1 : intro_idx]
    materials_line = body[body.rfind("\n", 0, materials_idx) + 1 : materials_idx]
    assert len(materials_line) > len(intro_line)


def test_toc_hints_round_trip_and_scope_is_a_slug(
    handler: TexHandler, tex_root: Path
) -> None:
    """hint-audit item 2: the TOC's ``Next:`` trailer must advertise
    ``scope='<file-slug>'`` — tex resolves search scope via
    ``ensure_ingested`` (a path/cite_key lookup), so the ``tx42``
    handle form 404s."""
    _write(
        tex_root,
        "paper.tex",
        r"\section{Introduction}" + "\n\n" + "Body of intro.\n",
    )
    out = handler.get(id="paper", view="toc")

    def dispatch(verb: str, kwargs: dict) -> object:
        kwargs.pop("kind", None)
        return getattr(handler, verb)(**kwargs)

    # The trailer's ``get(id='<universal-handle>')`` "overview" hint
    # self-identifies its kind only through the FULL runtime dispatch
    # layer (``_maybe_infer_kind_from_handle`` in ``runtime/dispatch.py``)
    # — calling the handler directly (as this test does, mirroring
    # ``test_chunk_trailer_hints_round_trip``'s pattern) can't resolve
    # it and isn't part of this fix anyway; restrict execution to the
    # ``search`` hint this test is actually about, so every hint still
    # PARSES but only the fixed one is dispatched.
    hints = assert_hints_round_trip(out.body, dispatch, execute_verbs=("search",))
    scope_hint = next(h for h in hints if h.startswith("search("))
    assert "scope='paper'" in scope_hint


def test_toc_view_path_form_works(handler: TexHandler, tex_root: Path) -> None:
    """``id='slug/toc'`` is equivalent to ``view='toc'``."""
    _write(tex_root, "doc.tex", r"\section{Hello}" + "\n")
    out_path = handler.get(id="doc/toc")
    out_kw = handler.get(id="doc", view="toc")
    assert out_path.body == out_kw.body


def test_toc_view_empty_when_no_sections(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "notes.tex",
        "Just a paragraph with no sectioning at all.\n",
    )
    out = handler.get(id="notes", view="toc")
    assert "no sectioning commands found" in out.body


# ── \input{} recursion (Phase C) ─────────────────────────────────────


def test_toc_recursively_expands_input(handler: TexHandler, tex_root: Path) -> None:
    """A \\input{chapters/intro} in main.tex must show intro.tex's
    sections inline at that position in the TOC."""
    _write(
        tex_root,
        "main.tex",
        r"\section{Top}"
        + "\n\n"
        + r"\input{chapters/intro}"
        + "\n\n"
        + r"\section{Conclusion}"
        + "\n",
    )
    _write(
        tex_root,
        "chapters/intro.tex",
        r"\section{Introduction}" + "\n\n" + r"\subsection{Background}" + "\n",
    )
    out = handler.get(id="main", view="toc")
    body = out.body
    # The parent's section appears.
    assert r"\section{Top}" in body
    # The child file's content appears too.
    assert r"\section{Introduction}" in body
    assert r"\subsection{Background}" in body
    # The \input{} marker is shown so the agent sees the boundary.
    assert "input{chapters/intro}" in body
    # Order: Top → input marker → child sections → Conclusion.
    top_idx = body.index(r"\section{Top}")
    input_idx = body.index("input{chapters/intro}")
    child_idx = body.index(r"\section{Introduction}")
    concl_idx = body.index(r"\section{Conclusion}")
    assert top_idx < input_idx < child_idx < concl_idx


def test_toc_handles_input_with_explicit_extension(
    handler: TexHandler, tex_root: Path
) -> None:
    """``\\input{foo.tex}`` (with extension) must resolve too."""
    _write(
        tex_root,
        "main.tex",
        r"\input{intro.tex}" + "\n",
    )
    _write(tex_root, "intro.tex", r"\section{Intro}" + "\n")
    out = handler.get(id="main", view="toc")
    assert r"\section{Intro}" in out.body


def test_toc_marks_missing_input_target(handler: TexHandler, tex_root: Path) -> None:
    _write(tex_root, "main.tex", r"\input{does-not-exist}" + "\n")
    out = handler.get(id="main", view="toc")
    assert "input{does-not-exist}" in out.body
    assert "not found" in out.body


# ── gr659000: /toc expands stored children whose files are off-disk ──
#
# Prod refs are written by workers whose workspaces live on other
# hosts. A ``\input{}`` child that isn't under *this* root still has
# a live stored ref — the TOC must walk its stored sections rather
# than report "not found". The child's resolution order keeps the
# disk authoritative (a real ``intro.tex`` outranks its stored ref)
# and matches the ingest-recorded ``meta['path']`` as well as the
# reconstructed slug.


def test_toc_expands_stored_input_child_off_disk(
    handler: TexHandler, tex_root: Path
) -> None:
    """The child's file is gone from this root but its stored ref is
    live: the marker says ``stored`` and its sections still inline."""
    _write(
        tex_root,
        "main.tex",
        r"\section{Top}" + "\n\n" + r"\input{chapters/intro}" + "\n",
    )
    child = _write(
        tex_root,
        "chapters/intro.tex",
        r"\section{Introduction}" + "\n\n" + "Intro body.\n",
    )
    handler.get(id="main", view="toc")  # ingest both
    child.unlink()
    body = handler.get(id="main", view="toc").body
    assert "not found" not in body
    assert r"⤷ \input{chapters/intro} → chapters--intro" in body
    assert "stored — file not on disk" in body
    assert r"\section{Introduction}" in body
    assert handler.store.get_ref(kind="tex", id="chapters--intro") is not None


def test_toc_expands_stored_child_when_parent_also_off_disk(
    handler: TexHandler, tex_root: Path
) -> None:
    """The prod gr659000 shape: BOTH files are off-disk — the parent's
    ``meta['path']`` still locates the child's ingest-recorded relpath
    so the TOC renders the whole stored tree."""
    main = _write(
        tex_root,
        "proj/main.tex",
        r"\section{Top}" + "\n\n" + r"\input{tex/intro}" + "\n",
    )
    child = _write(
        tex_root,
        "proj/tex/intro.tex",
        r"\section{Stored Intro}" + "\n",
    )
    handler.get(id="proj--main", view="toc")  # ingest both
    main.unlink()
    child.unlink()
    body = handler.get(id="proj--main", view="toc").body
    assert "not found" not in body
    assert "proj--tex--intro" in body
    assert r"\section{Stored Intro}" in body


def test_toc_resolves_stored_child_by_meta_path(
    handler: TexHandler, tex_root: Path
) -> None:
    """A stored child whose slug isn't the relpath's slug encoding
    still resolves: ``meta['path']`` is the authoritative ingest-time
    relpath (the path→slug encoding is lossy)."""
    from precis.store.types import ChunkInsert

    _write(tex_root, "main.tex", r"\input{chapters/intro}" + "\n")
    ref = handler.store.insert_ref(
        kind="tex",
        slug="legacy-intro",
        title="Intro",
        meta={"path": "chapters/intro.tex", "ext": ".tex"},
    )
    handler.store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=r"\section{Legacy Intro}",
                slug="legacy-intro",
                meta={"section_level": 0, "section_title": "Legacy Intro"},
            )
        ],
    )
    body = handler.get(id="main", view="toc").body
    assert "not found" not in body
    assert "legacy-intro" in body
    assert r"\section{Legacy Intro}" in body


def test_toc_on_disk_child_wins_over_stored_ref(
    handler: TexHandler, tex_root: Path
) -> None:
    """An on-disk ``intro.tex`` must outrank a stored ref claiming the
    same slug — the extensionless ``\\input{intro}`` candidate
    slug-matches it, which must not shadow the real file."""
    _write(tex_root, "main.tex", r"\input{intro}" + "\n")
    stored = _write(tex_root, "intro.tex", r"\section{Old}" + "\n")
    handler.get(id="main", view="toc")  # ingest both
    stored.write_text(r"\section{Fresh}" + "\n", encoding="utf-8")
    body = handler.get(id="main", view="toc").body
    assert r"\section{Fresh}" in body
    assert "stored — file not on disk" not in body


def test_toc_breaks_input_cycles(handler: TexHandler, tex_root: Path) -> None:
    """``a.tex`` includes ``b.tex`` includes ``a.tex`` — the walker
    must terminate with a cycle marker, not recurse forever."""
    _write(
        tex_root,
        "a.tex",
        r"\section{A}" + "\n" + r"\input{b}" + "\n",
    )
    _write(
        tex_root,
        "b.tex",
        r"\section{B}" + "\n" + r"\input{a}" + "\n",
    )
    out = handler.get(id="a", view="toc")
    body = out.body
    assert r"\section{A}" in body
    assert r"\section{B}" in body
    assert "cycle" in body


def test_toc_unknown_view_still_rejected(handler: TexHandler, tex_root: Path) -> None:
    """Confirm /toc was added without breaking the unsupported-view
    error path for other names."""
    _write(tex_root, "x.tex", r"\section{x}" + "\n")
    from precis.errors import Unsupported

    with pytest.raises(Unsupported, match="unknown tex view"):
        handler.get(id="x", view="bogus")


def test_extensionless_path_resolves_as_file(
    handler: TexHandler, tex_root: Path
) -> None:
    """``tex/graphene`` (extensionless slash-path) addresses the file, not a
    bogus ``graphene`` view — the top plan-tick workspace-authoring confusion
    (prod 2026-07-03). It must read the same file as the ``--`` slug form."""
    _write(tex_root, "tex/graphene.tex", r"\section{Graphene}" + "\n")
    by_path = handler.get(id="tex/graphene").body
    by_slug = handler.get(id="tex--graphene").body
    assert r"\section{Graphene}" in by_path
    assert by_path == by_slug


def test_nested_extensionless_path_resolves(
    handler: TexHandler, tex_root: Path
) -> None:
    """A multi-segment extensionless path is unambiguously a file path."""
    _write(tex_root, "projects/proj/tex/graphene.tex", r"\section{G}" + "\n")
    out = handler.get(id="projects/proj/tex/graphene").body
    assert r"\section{G}" in out


# ── \input{} write-access safety (Phase D) ───────────────────────────


def test_input_outside_root_silently_dropped(
    handler: TexHandler, tex_root: Path, tmp_path: Path
) -> None:
    """An ``\\input{...}`` whose resolved target escapes ``self.root``
    must NOT be followed. The TOC marks it as not-found rather than
    leaking content from outside PRECIS_ROOT."""
    # Put a fake target outside the root the handler walks.
    outside = tmp_path / "outside-root"
    outside.mkdir()
    (outside / "secret.tex").write_text(
        r"\section{SECRET DATA}" + "\n", encoding="utf-8"
    )

    _write(tex_root, "main.tex", r"\input{../outside-root/secret}" + "\n")
    out = handler.get(id="main", view="toc")
    body = out.body
    # The ``\input{}`` line itself is reported, but the target is not
    # resolved — so SECRET DATA never lands in the rendered TOC.
    assert "SECRET DATA" not in body
    assert "not found" in body


def test_existing_blocks_stay_in_root_scope(
    handler: TexHandler, tex_root: Path, tmp_path: Path
) -> None:
    """Sanity: even after a path-traversal attempt at ``\\input``,
    a follow-up ``put`` to a normal slug still works inside root."""
    outside = tmp_path / "ext"
    outside.mkdir()
    (outside / "x.tex").write_text("X", encoding="utf-8")

    _write(tex_root, "a.tex", r"\input{../ext/x}" + "\n")
    handler.get(id="a", view="toc")  # should not crash
    # Now a legit write inside root succeeds.
    handler.put(id="b", text=r"\section{Inside}" + "\n", mode="create")
    assert (tex_root / "b.tex").exists()
    # And the rogue file was never written into our root.
    assert not (tex_root / "ext").exists()


def test_handler_root_is_resolved_to_absolute(store, tmp_path: Path) -> None:
    """``self.root`` must be canonicalised at construction time so the
    ``relative_to`` check in ``_resolve_path`` doesn't fall over for
    relative inputs."""
    rel = tmp_path / "subdir"
    rel.mkdir()
    h = TexHandler(hub=Hub(store=store), root=rel)
    assert h.root.is_absolute()
    assert h.root == rel.resolve()


# ── /toc ingest budget + embed-failure degrade (gr311327) ─────────────
#
# A large \input tree used to make ``/toc`` call ``ensure_ingested``
# once per child, synchronously, on the request thread — each a full
# parse + store write + (formerly unguarded) embedder call. One flaky
# embedder call anywhere in a ~100-file tree raised straight out of the
# walk; a healthy-but-slow embedder just made the call serially slow.
# These tests pin the two-part fix: (1) a per-walk ingest budget that
# renders "not yet indexed" markers instead of ingesting past the cap,
# and (2) an ingest-time embed guard (shared with every other
# ``ensure_ingested`` caller via ``to_chunk_inserts``) that degrades to
# unembedded chunks instead of raising.


class _RaisingBatchEmbedder(MockEmbedder):
    """Fake embedder whose batch ``embed()`` always raises.

    Mirrors ``tests/test_embed_query.py``'s ``_RaisingBatchEmbedder``
    (the read-path twin) — this is the ingest-path shape.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("remote embed endpoint down")

    def embed_one(self, text: str) -> list[float]:
        raise RuntimeError("remote embed endpoint down")


def _write_input_tree(root: Path, *, n_children: int) -> None:
    """A ``main.tex`` that ``\\input{}``s ``n_children`` never-before-
    ingested sibling files, each with its own section."""
    inputs = "\n\n".join(f"\\input{{child{i}}}" for i in range(n_children))
    _write(root, "main.tex", r"\section{Root}" + "\n\n" + inputs + "\n")
    for i in range(n_children):
        _write(root, f"child{i}.tex", f"\\section{{Child {i}}}\n")


def test_toc_caps_ingests_and_marks_pending(
    handler: TexHandler, tex_root: Path
) -> None:
    """25 never-ingested \\input children on a handler capped at
    ``MAX_TOC_INGESTS=20`` (the default): the walk completes (doesn't
    hang/raise), the first 20 children are indexed inline, the
    remaining 5 render as "not yet indexed" markers, and the response
    ends with a trailer naming the count."""
    n = 25
    _write_input_tree(tex_root, n_children=n)

    out = handler.get(id="main", view="toc")
    body = out.body

    assert r"\section{Root}" in body
    # Indexed children show their real section title inline; pending
    # ones show only the "not yet indexed" marker for their slug.
    indexed = sum(1 for i in range(n) if f"Child {i}" in body)
    pending = sum(1 for i in range(n) if f"child{i} (not yet indexed)" in body)
    assert indexed == TexHandler.MAX_TOC_INGESTS
    assert pending == n - TexHandler.MAX_TOC_INGESTS
    assert f"{n - TexHandler.MAX_TOC_INGESTS} file(s) not yet indexed" in body
    assert "re-run the toc view" in body


def test_toc_second_call_indexes_more(handler: TexHandler, tex_root: Path) -> None:
    """A follow-up ``/toc`` call must make progress: children indexed
    by the first call are free (mtime-current) on the second call, so
    the fresh budget goes entirely toward previously-pending children."""
    n = 25
    _write_input_tree(tex_root, n_children=n)

    first = handler.get(id="main", view="toc").body
    assert "not yet indexed" in first

    second = handler.get(id="main", view="toc").body
    for i in range(n):
        assert f"Child {i}" in second
    assert "not yet indexed" not in second


def test_toc_within_cap_is_unaffected(handler: TexHandler, tex_root: Path) -> None:
    """Sanity: a tree smaller than the cap renders every child inline
    on the first call, with no pending trailer — the cap must not
    change behavior for the common case."""
    n = 3
    _write_input_tree(tex_root, n_children=n)
    body = handler.get(id="main", view="toc").body
    for i in range(n):
        assert f"Child {i}" in body
    assert "not yet indexed" not in body


def test_toc_survives_raising_embedder_with_unembedded_chunks(
    store, tex_root: Path
) -> None:
    """A wired-but-failing embedder must not abort the TOC walk (raw
    ``EmbedderUnavailable``/``RuntimeError`` leak) or leave a
    partially-ingested tree behind an unhandled exception. Ingested
    chunks (both the root file and its \\input child) land with
    ``embedding IS NULL`` instead — a legal row state the fill-cascade
    backfills later."""
    handler = TexHandler(
        hub=Hub(store=store, embedder=_RaisingBatchEmbedder()), root=tex_root
    )
    _write_input_tree(tex_root, n_children=3)

    out = handler.get(id="main", view="toc")
    body = out.body
    assert r"\section{Root}" in body
    for i in range(3):
        assert f"Child {i}" in body

    root_ref = store.get_ref(kind="tex", id="main")
    assert root_ref is not None
    child_ref = store.get_ref(kind="tex", id="child0")
    assert child_ref is not None

    with store.pool.connection() as conn:
        for ref in (root_ref, child_ref):
            for block in store.chunks.list_chunks_for_ref(ref.id):
                row = conn.execute(
                    "SELECT 1 FROM chunk_embeddings WHERE chunk_id = %s",
                    (block.id,),
                ).fetchone()
                assert row is None, (
                    f"expected no chunk_embeddings row for {ref.slug}~{block.slug} "
                    "(degrade-on-embed-failure should leave it unembedded)"
                )


def test_toc_residual_db_error_names_the_child(
    handler: TexHandler, tex_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-embedder failure during a child ingest (e.g. a DB error)
    must surface as ONE typed error naming the offending child file,
    not a raw internal exception leaking out of the walk."""
    from precis.errors import Upstream

    _write(
        tex_root,
        "main.tex",
        r"\section{Root}" + "\n\n" + r"\input{broken}" + "\n",
    )
    _write(tex_root, "broken.tex", r"\section{Broken}" + "\n")

    real_ensure_ingested = TexHandler.ensure_ingested

    def boom(self, slug, *, force=False, missing_ok=False):
        if slug == "broken":
            raise RuntimeError("connection reset by peer")
        return real_ensure_ingested(self, slug, force=force, missing_ok=missing_ok)

    monkeypatch.setattr(TexHandler, "ensure_ingested", boom)

    with pytest.raises(Upstream, match="broken"):
        handler.get(id="main", view="toc")


# ── gr660623: delete/get must resolve the same tombstone ─────────────
#
# Observed on 2026-11-13: delete(kind='tex', id=<slug>) answered
# NotFound yet the ref ended up with retired_at set — a partial write
# the caller couldn't see — while get on a retired slug answered the
# same NotFound a typo would. The door now resolves the ref
# include_deleted so a tombstone reports Gone, a retire that loses a
# race reports Gone (not a bare NotFound beside a retired ref), and
# the ack names exactly which state changed.


def test_delete_file_ack_names_retired_ref(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """A whole-file delete must ack what it did — both sides of the
    mutation are named so the caller can tell a file+ref retire from
    a file-only or ref-only one."""
    _write(tex_root, "intro.tex", r"\section{Intro}" + "\n")
    handler.get(id="intro")
    ref = store.get_ref(kind="tex", id="intro")
    assert ref is not None
    resp = handler.delete(id="intro", confirm="delete-file-intro")
    assert "deleted file" in resp.body
    assert f"ref id={ref.id}" in resp.body
    assert not (tex_root / "intro.tex").exists()
    tomb = store.get_ref(kind="tex", id="intro", include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None


def test_delete_already_retired_reports_gone(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """gr660623: a second delete on the same slug used to raise the
    same NotFound a typo would — after the first delete the slug is a
    tombstone, so it must report Gone and change nothing."""
    _write(tex_root, "gone.tex", r"\section{G}" + "\n")
    handler.get(id="gone")
    handler.delete(id="gone", confirm="delete-file-gone")
    ref = store.get_ref(kind="tex", id="gone", include_deleted=True)
    assert ref is not None and ref.retired_at is not None
    with pytest.raises(Gone, match="already deleted"):
        handler.delete(id="gone", confirm="delete-file-gone")


def test_get_retired_ref_with_file_on_disk_reports_gone(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """td661027 review of gr660623: a retired ref whose file is STILL on
    disk (a delete whose unlink failed after the restore died, or a bare
    retire) must not be silently re-ingested — ``Store.insert_ref``
    reclaims the slug from the retired owner for a new live ref, which
    would make get serve a fresh ref while delete on the same slug
    reports Gone. The tombstone probe runs before ingestion, so get
    answers Gone and the slug stays with the tombstone."""
    _write(tex_root, "ghost.tex", r"\section{G}" + "\n")
    handler.get(id="ghost")
    ref = store.get_ref(kind="tex", id="ghost")
    assert ref is not None
    store.retire_ref(ref.id)  # ref retired; the file stays on disk
    assert (tex_root / "ghost.tex").exists()
    with pytest.raises(Gone, match="deleted"):
        handler.get(id="ghost")
    tomb = store.get_ref(kind="tex", id="ghost", include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None
    assert tomb.id == ref.id, "the tombstone must keep the slug"
    assert store.get_ref(kind="tex", id="ghost") is None, (
        "get must not have re-ingested the on-disk file into a new live ref"
    )
    assert (tex_root / "ghost.tex").exists(), "get must not touch the file"


def test_get_on_retired_ref_reports_gone(handler: TexHandler, tex_root: Path) -> None:
    """gr660623: get and delete must resolve the same slug the same
    way. After a delete retires the ref, get must answer Gone (the
    tombstone delete reports), not the suggestions-rich NotFound."""
    _write(tex_root, "gone.tex", r"\section{G}" + "\n")
    handler.get(id="gone")
    handler.delete(id="gone", confirm="delete-file-gone")
    with pytest.raises(Gone, match="deleted"):
        handler.get(id="gone")


def test_delete_off_disk_ref_retires_and_says_so(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """A live ref whose file isn't under this root (foreign workspace)
    can still be deleted by slug — the ack must name the ref retire
    and admit no file was removed."""
    _write(tex_root, "remote.tex", r"\section{R}" + "\n")
    handler.get(id="remote")
    ref = store.get_ref(kind="tex", id="remote")
    assert ref is not None
    (tex_root / "remote.tex").unlink()  # file "leaves" this root
    resp = handler.delete(id="remote", confirm="delete-file-remote")
    assert "retired" in resp.body
    assert f"ref id={ref.id}" in resp.body
    assert "no file" in resp.body
    tomb = store.get_ref(kind="tex", id="remote", include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None


def test_delete_racing_retire_reports_gone(
    handler: TexHandler,
    tex_root: Path,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If retire_ref loses a race (another writer retired between our
    resolve and the retire), delete must report the tombstone — Gone —
    not a NotFound that hides a completed retire (gr660623's exact
    failure mode). The retire now runs BEFORE the file unlink
    (td661027 review), so a lost race must also leave the file on
    disk: the call changed nothing the caller can't see."""
    _write(tex_root, "raced.tex", r"\section{R}" + "\n")
    handler.get(id="raced")

    real_retire_ref = store.retire_ref

    def racing_retire(ref_id, *, conn=None):
        # Truthful race: the other writer's retire LANDS, ours then
        # finds the tombstone.
        real_retire_ref(ref_id, conn=conn)
        raise NotFound("ref is already retired")

    monkeypatch.setattr(store, "retire_ref", racing_retire)
    with pytest.raises(Gone):
        handler.delete(id="raced", confirm="delete-file-raced")
    assert (tex_root / "raced.tex").exists(), (
        "a delete that raises Gone must not have unlinked the file"
    )
    tomb = store.get_ref(kind="tex", id="raced", include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None


def test_delete_unlink_failure_restores_ref(
    handler: TexHandler,
    tex_root: Path,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlink failure after a successful retire must roll the ref
    back (restore_ref) and report that NOTHING was deleted — otherwise
    the caller retries into a tombstone while the file is still there
    (td661027 review of gr660623)."""
    _write(tex_root, "stuck.tex", r"\section{S}" + "\n")
    handler.get(id="stuck")

    def boom(path):
        raise OSError("operation not permitted")

    monkeypatch.setattr("os.remove", boom)
    with pytest.raises(Upstream, match="nothing was deleted"):
        handler.delete(id="stuck", confirm="delete-file-stuck")
    assert (tex_root / "stuck.tex").exists()
    ref = store.get_ref(kind="tex", id="stuck")
    assert ref is not None and ref.retired_at is None, (
        "the ref retire must be rolled back when the unlink fails"
    )


def test_delete_unlink_failure_restore_fails_names_both_mutations(
    handler: TexHandler,
    tex_root: Path,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Double failure (unlink fails AND restore_ref dies): the error
    must truthfully name both mutations — ref retired, file still on
    disk — instead of claiming a clean rollback or a clean delete."""
    _write(tex_root, "wedged.tex", r"\section{W}" + "\n")
    handler.get(id="wedged")

    def boom_remove(path):
        raise OSError("operation not permitted")

    def boom_restore(ref_id, *, conn=None):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr("os.remove", boom_remove)
    monkeypatch.setattr(store, "restore_ref", boom_restore)
    with pytest.raises(Upstream, match="could not be rolled back"):
        handler.delete(id="wedged", confirm="delete-file-wedged")
    assert (tex_root / "wedged.tex").exists()
    tomb = store.get_ref(kind="tex", id="wedged", include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None


# ── td661027 review of gr660623: read paths must not resurrect ──────
#
# get and delete probe the tombstone via ``_require_existing_file``,
# but two read paths still called ``ensure_ingested`` directly:
# scoped ``search`` (plaintext.search) and the ``/toc`` walker's
# ``\\input{}`` resolver. On a retired ref whose file is still on
# disk either one silently re-ingested — ``Store.insert_ref``
# reclaims the slug for a NEW live ref — so the caller that just saw
# ``Gone`` from delete would find the "deleted" file alive again.


def test_search_scope_on_tombstone_reports_gone(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """A scoped search whose scope slug is a tombstone must report
    Gone — the same answer get and delete give — instead of
    re-ingesting the on-disk file into a fresh live ref."""
    _write(tex_root, "doc.tex", r"\section{Doc}" + "\n\nneedle body\n")
    handler.get(id="doc")
    ref = store.get_ref(kind="tex", id="doc")
    assert ref is not None
    store.retire_ref(ref.id)  # tombstone; the file stays on disk
    assert (tex_root / "doc.tex").exists()
    with pytest.raises(Gone, match="deleted"):
        handler.search(q="needle", scope="doc")
    tomb = store.get_ref(kind="tex", id="doc", include_deleted=True)
    assert tomb is not None and tomb.id == ref.id, "the tombstone must keep the slug"
    assert store.get_ref(kind="tex", id="doc") is None, (
        "search must not have re-ingested the file into a new live ref"
    )
    assert (tex_root / "doc.tex").exists(), "search must not touch the file"


def test_toc_marks_on_disk_tombstone_child_deleted(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """A ``\\input{}`` child whose ref is retired but whose file is
    still on disk renders a "(deleted)" marker — the walker must not
    resurrect it into a new live ref."""
    _write(
        tex_root,
        "main.tex",
        r"\section{Root}" + "\n\n" + r"\input{ghost}" + "\n",
    )
    _write(tex_root, "ghost.tex", r"\section{GhostChild}" + "\n")
    handler.get(id="ghost")
    ref = store.get_ref(kind="tex", id="ghost")
    assert ref is not None
    store.retire_ref(ref.id)  # tombstone; the file stays on disk

    body = handler.get(id="main", view="toc").body
    assert r"\input{ghost} → ghost (deleted)" in body
    assert "GhostChild" not in body, "the dead child's sections must not be expanded"
    tomb = store.get_ref(kind="tex", id="ghost", include_deleted=True)
    assert tomb is not None and tomb.id == ref.id, "the tombstone must keep the slug"
    assert store.get_ref(kind="tex", id="ghost") is None, (
        "the toc walk must not have re-ingested the file"
    )
    assert (tex_root / "ghost.tex").exists()


def test_toc_marks_off_disk_tombstone_child_deleted(
    handler: TexHandler, tex_root: Path, store: Store
) -> None:
    """A ``\\input{}`` child that is a tombstone AND has no file under
    this root still answers "(deleted)", not the "not found" marker a
    typo would — pass 2 of the resolver probes the tombstone too."""
    _write(
        tex_root,
        "main.tex",
        r"\section{Root}" + "\n\n" + r"\input{gone}" + "\n",
    )
    _write(tex_root, "gone.tex", r"\section{GoneChild}" + "\n")
    handler.get(id="gone")
    ref = store.get_ref(kind="tex", id="gone")
    assert ref is not None
    store.retire_ref(ref.id)
    (tex_root / "gone.tex").unlink()

    body = handler.get(id="main", view="toc").body
    assert r"\input{gone} → gone (deleted)" in body
    assert "not found" not in body


def test_toc_still_expands_live_child(handler: TexHandler, tex_root: Path) -> None:
    """Guard the guard: a live ``\\input{}`` child must still resolve
    and expand inline — the tombstone probe only fires on retired
    refs, never on a live or never-ingested slug."""
    _write(
        tex_root,
        "main.tex",
        r"\section{Root}" + "\n\n" + r"\input{alive}" + "\n",
    )
    _write(tex_root, "alive.tex", r"\section{AliveChild}" + "\n")
    body = handler.get(id="main", view="toc").body
    assert "AliveChild" in body
    assert "(deleted)" not in body
