"""Tests for MarkdownHandler — phase 6 file kind."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.markdown import MarkdownHandler
from precis.store import Store


@pytest.fixture
def md_root(tmp_path: Path) -> Path:
    """Empty markdown root for tests."""
    return tmp_path


@pytest.fixture
def handler(store: Store, md_root: Path) -> MarkdownHandler:
    return MarkdownHandler(store=store, root=md_root)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── construction ─────────────────────────────────────────────────────


def test_construction_fails_on_missing_root(store: Store, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        MarkdownHandler(store=store, root=tmp_path / "no-such-dir")


def test_construction_fails_on_file_root(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "f.md"
    f.write_text("hi")
    with pytest.raises(ValueError, match="not a directory"):
        MarkdownHandler(store=store, root=f)


# ── index view ───────────────────────────────────────────────────────


def test_empty_root_lists_no_files(handler: MarkdownHandler) -> None:
    out = handler.get()
    assert "no markdown files found" in out.body


def test_index_lists_files(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "alpha.md", "# A")
    _write(md_root, "beta.md", "# B")
    _write(md_root, "notes/meeting.md", "# Meeting")
    out = handler.get()
    assert "3 markdown file(s)" in out.body
    assert "alpha" in out.body
    assert "beta" in out.body
    assert "notes--meeting" in out.body


def test_path_form_index(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "x.md", "# X")
    out = handler.get(id="/")
    assert "1 markdown file" in out.body


# ── overview ─────────────────────────────────────────────────────────


def test_overview_renders_metadata(handler: MarkdownHandler, md_root: Path) -> None:
    _write(
        md_root,
        "foo.md",
        "# Foo Title\n\nIntro paragraph.\n\n## Sub\n\nMore.\n",
    )
    out = handler.get(id="foo")
    assert "foo" in out.body
    assert "Foo Title" in out.body  # used as title
    assert "blocks:" in out.body
    assert "path:" in out.body
    # The TOC preview should surface the headings.
    assert "Foo Title" in out.body
    assert "Sub" in out.body


def test_overview_for_missing_file_raises(handler: MarkdownHandler) -> None:
    with pytest.raises(NotFound, match="not found"):
        handler.get(id="nonexistent")


# ── block-by-slug navigation ─────────────────────────────────────────


def test_get_block_by_slug(handler: MarkdownHandler, md_root: Path) -> None:
    _write(
        md_root,
        "doc.md",
        "# Title\n\nThe quick brown fox jumps.\n\n## Section\n\nMore content.\n",
    )
    # Force ingest by getting overview first.
    handler.get(id="doc")
    # The first paragraph's slug is content-derived.
    out = handler.get(id="doc/toc")
    assert "Title" in out.body and "Section" in out.body

    # Heading slug is predictable.
    out = handler.get(id="doc~title")
    assert "Title" in out.body
    out = handler.get(id="doc~section")
    assert "Section" in out.body


def test_get_block_by_pos(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# H\n\nP1.\n\nP2.\n")
    out = handler.get(id="doc~0")
    assert "# H" in out.body
    out = handler.get(id="doc~1")
    assert "P1" in out.body


def test_missing_block_raises(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# H\n")
    with pytest.raises(NotFound, match="no block"):
        handler.get(id="doc~nonexistent")
    with pytest.raises(NotFound, match="no block"):
        handler.get(id="doc~99")


# ── views ─────────────────────────────────────────────────────────────


def test_toc_view(handler: MarkdownHandler, md_root: Path) -> None:
    _write(
        md_root,
        "doc.md",
        "# Top\n\nIntro.\n\n## A\n\nA body.\n\n## B\n\nB body.\n",
    )
    out = handler.get(id="doc/toc")
    assert "Top" in out.body
    assert "A" in out.body and "B" in out.body


def test_raw_view(handler: MarkdownHandler, md_root: Path) -> None:
    body = "# Title\n\nHello.\n"
    _write(md_root, "doc.md", body)
    out = handler.get(id="doc/raw")
    assert out.body == body


def test_unknown_view_raises(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# H\n")
    with pytest.raises(Unsupported):
        handler.get(id="doc/unknownview")


def test_view_plus_block_selector_rejected(
    handler: MarkdownHandler, md_root: Path
) -> None:
    _write(md_root, "doc.md", "# H\n")
    with pytest.raises(BadInput, match="cannot combine"):
        handler.get(id="doc~h/toc")


# ── lazy re-ingest ───────────────────────────────────────────────────


def test_lazy_reingest_picks_up_changes(
    handler: MarkdownHandler, md_root: Path
) -> None:
    p = _write(md_root, "doc.md", "# Original\n\nOld content.\n")
    handler.get(id="doc")  # ingest
    # Wait so mtime_ns differs even on coarse-grained filesystems.
    time.sleep(0.01)
    p.write_text("# Updated\n\nNew content here.\n", encoding="utf-8")
    out = handler.get(id="doc")
    assert "Updated" in out.body
    # Old paragraph slug should be gone; new one present.
    out2 = handler.get(id="doc/raw")
    assert "Updated" in out2.body
    assert "Old content" not in out2.body


def test_unchanged_file_keeps_ref(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# Stable\n\nContent.\n")
    out1 = handler.get(id="doc")
    ref1 = handler.store.get_ref(kind="markdown", id="doc")
    assert ref1 is not None
    out2 = handler.get(id="doc")
    ref2 = handler.store.get_ref(kind="markdown", id="doc")
    assert ref2 is not None
    assert ref1.id == ref2.id  # same ref, no churn
    assert out1.body == out2.body


def test_deleted_file_soft_deletes_ref(handler: MarkdownHandler, md_root: Path) -> None:
    p = _write(md_root, "ephemeral.md", "# X\n")
    handler.get(id="ephemeral")
    p.unlink()
    with pytest.raises(NotFound):
        handler.get(id="ephemeral")
    # Soft-deleted refs are filtered out by get_ref.
    assert handler.store.get_ref(kind="markdown", id="ephemeral") is None


# ── search ───────────────────────────────────────────────────────────


def test_search_blocks(handler: MarkdownHandler, md_root: Path) -> None:
    _write(
        md_root,
        "doc.md",
        "# Title\n\nThe quick brown fox jumps.\n\n## Sub\n\nUnique snowflake content.\n",
    )
    handler.get(id="doc")  # ingest
    out = handler.get(id="doc")  # ensure
    out = handler.search(q="snowflake")
    assert "snowflake" in out.body.lower()
    assert "1 block hit" in out.body or "block hit" in out.body


def test_search_no_match(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# H\n\nHello.\n")
    handler.get(id="doc")
    out = handler.search(q="zzzfrobnicate")
    assert "no markdown blocks match" in out.body


def test_search_with_scope(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "a.md", "# A\n\nApple banana.\n")
    _write(md_root, "b.md", "# B\n\nApple cherry.\n")
    handler.get(id="a")
    handler.get(id="b")
    out = handler.search(q="banana", scope="a")
    assert "block hit" in out.body
    out_b = handler.search(q="banana", scope="b")
    assert "no markdown blocks match" in out_b.body


def test_search_requires_query(handler: MarkdownHandler) -> None:
    with pytest.raises(BadInput):
        handler.search()


# ── put: create ──────────────────────────────────────────────────────


def test_put_create(handler: MarkdownHandler, md_root: Path) -> None:
    out = handler.put(
        id="newfile",
        text="# New File\n\nFirst content.\n",
        mode="create",
    )
    assert "created markdown" in out.body
    assert (md_root / "newfile.md").exists()
    # Content preserved.
    out = handler.get(id="newfile/raw")
    assert "New File" in out.body


def test_put_create_rejects_existing(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "existing.md", "# X\n")
    with pytest.raises(BadInput, match="already exists"):
        handler.put(id="existing", text="# Y\n", mode="create")


# ── put: append ──────────────────────────────────────────────────────


def test_put_append(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# Doc\n\nFirst para.\n")
    handler.put(id="doc", text="Appended paragraph.", mode="append")
    raw = (md_root / "doc.md").read_text()
    assert "First para." in raw
    assert "Appended paragraph." in raw
    # The block list should now have the new paragraph.
    handler.get(id="doc")  # re-fetch to confirm parse worked
    out = handler.search(q="Appended")
    assert "Appended" in out.body


def test_put_append_requires_text(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# X\n")
    with pytest.raises(BadInput, match="requires text"):
        handler.put(id="doc", mode="append")


# ── put: replace ─────────────────────────────────────────────────────


def test_put_replace_block(handler: MarkdownHandler, md_root: Path) -> None:
    _write(
        md_root,
        "doc.md",
        "# Title\n\nOriginal paragraph.\n\n## Sub\n\nKeep me.\n",
    )
    handler.get(id="doc")  # ingest first so we know the slug
    # Find the slug for the "Original paragraph" block.
    ref = handler.store.get_ref(kind="markdown", id="doc")
    assert ref is not None
    blocks = handler.store.list_blocks_for_ref(ref.id)
    para = next(b for b in blocks if b.text.startswith("Original"))

    handler.put(
        id=f"doc~{para.slug}",
        text="Replacement paragraph.",
        mode="replace",
    )
    raw = (md_root / "doc.md").read_text()
    assert "Replacement paragraph." in raw
    assert "Original paragraph." not in raw
    # Sibling content preserved.
    assert "Keep me." in raw
    assert "## Sub" in raw


def test_put_replace_requires_block_selector(
    handler: MarkdownHandler, md_root: Path
) -> None:
    _write(md_root, "doc.md", "# X\n\nA.\n")
    with pytest.raises(BadInput, match="block selector"):
        handler.put(id="doc", text="new", mode="replace")


def test_put_replace_unknown_block(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# X\n\nA.\n")
    handler.get(id="doc")
    with pytest.raises(NotFound, match="block.*not found"):
        handler.put(id="doc~nope", text="x", mode="replace")


# ── put: delete ──────────────────────────────────────────────────────


def test_put_delete_block(handler: MarkdownHandler, md_root: Path) -> None:
    _write(
        md_root,
        "doc.md",
        "# Title\n\nDelete me.\n\nKeep me.\n",
    )
    handler.get(id="doc")
    ref = handler.store.get_ref(kind="markdown", id="doc")
    assert ref is not None
    blocks = handler.store.list_blocks_for_ref(ref.id)
    target = next(b for b in blocks if b.text.startswith("Delete"))

    handler.put(id=f"doc~{target.slug}", mode="delete")
    raw = (md_root / "doc.md").read_text()
    assert "Delete me." not in raw
    assert "Keep me." in raw


def test_put_delete_requires_selector(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "doc.md", "# X\n")
    with pytest.raises(BadInput, match="block selector"):
        handler.put(id="doc", mode="delete")


# ── put: bad mode ────────────────────────────────────────────────────


def test_put_bad_mode(handler: MarkdownHandler) -> None:
    with pytest.raises(BadInput, match="mode= is required"):
        handler.put(id="doc", text="x")
    with pytest.raises(BadInput, match="mode= is required"):
        handler.put(id="doc", text="x", mode="bogus")


# ── path traversal defence ───────────────────────────────────────────


def test_invalid_slug_blocked(handler: MarkdownHandler) -> None:
    with pytest.raises(BadInput, match="invalid markdown slug"):
        handler.get(id="../etc/passwd")
    with pytest.raises(BadInput, match="invalid markdown slug"):
        handler.get(id="UPPERCASE")


# ── nested files ──────────────────────────────────────────────────────


def test_nested_dir_files(handler: MarkdownHandler, md_root: Path) -> None:
    _write(md_root, "deep/nested/file.md", "# Deep\n\nContent.\n")
    out = handler.get(id="deep--nested--file")
    assert "Deep" in out.body
