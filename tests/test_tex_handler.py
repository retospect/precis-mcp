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
    return tmp_path


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
    assert PlaintextHandler._EXTENSIONS == (".txt", ".log")


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
    assert "edited 1 span" in out.body
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
    assert "appended to tex" in out.body
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
