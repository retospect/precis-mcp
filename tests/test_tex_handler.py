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
from precis.errors import BadInput, NotFound
from precis.handlers.plaintext import PlaintextHandler
from precis.handlers.tex import TexHandler
from precis.store import Store


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
    f.write_text(r"\section{intro}")
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
    assert "no tex files found" in out.body


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
    assert out.body.startswith("edited block ")
    assert "'paper'" in out.body
    assert " (L" in out.body
    content = (tex_root / "paper.tex").read_text()
    assert "New Title" in content
    assert "Old Title" not in content


def test_edit_append(handler: TexHandler, tex_root: Path) -> None:
    _write(tex_root, "doc.tex", r"\section{One}" + "\n")
    out = handler.edit(
        id="doc",
        mode="append",
        text=r"\section{Two}",
    )
    assert out.body.startswith("appended block ")
    assert "'doc'" in out.body
    assert r"\section{Two}" in (tex_root / "doc.tex").read_text()


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
    with pytest.raises(BadInput, match="invalid tex slug"):
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
    blocks = handler.store.list_blocks_for_ref(ref.id)
    # Find each by content.
    methods = [b for b in blocks if b.text == r"\section{Methods}"][0]
    kinetics = [b for b in blocks if b.text == r"\subsection{Kinetics}"][0]
    body = [b for b in blocks if b.text.startswith("We measured")][0]

    assert methods.meta["section_level"] == 0
    assert methods.meta["section_title"] == "Methods"

    assert kinetics.meta["section_level"] == 1
    assert kinetics.meta["section_title"] == "Kinetics"
    # Subsection records its parent section.
    assert kinetics.meta["section_path"] == [[0, "Methods"]]

    # The body paragraph isn't a section heading itself but knows
    # which section ancestry it sits inside.
    assert "section_level" not in body.meta
    assert body.meta["section_path"] == [[0, "Methods"], [1, "Kinetics"]]


def test_inputs_stored_in_block_meta(handler: TexHandler, tex_root: Path) -> None:
    _write(
        tex_root,
        "main.tex",
        r"\section{Top}" + "\n\n" + r"\input{chapters/intro}" + "\n",
    )
    handler.get(id="main")
    ref = handler.store.get_ref(kind="tex", id="main")
    assert ref is not None
    blocks = handler.store.list_blocks_for_ref(ref.id)
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
    (outside / "secret.tex").write_text(r"\section{SECRET DATA}" + "\n")

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
    (outside / "x.tex").write_text("X")

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
