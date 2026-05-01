"""Tests for PlaintextHandler — the .txt / .log sibling of markdown."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.plaintext import PlaintextHandler
from precis.store import Store
from precis.utils.plaintext_parse import parse_plaintext


@pytest.fixture
def pt_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def handler(hub: Hub, pt_root: Path) -> PlaintextHandler:
    return PlaintextHandler(hub=hub, root=pt_root)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── parser ────────────────────────────────────────────────────────────


def test_parser_splits_on_blank_lines() -> None:
    text = "alpha one\nalpha two\n\nbeta only\n\n\ngamma\n"
    blocks = parse_plaintext(text)
    assert len(blocks) == 3
    assert blocks[0].text == "alpha one\nalpha two"
    assert blocks[1].text == "beta only"
    assert blocks[2].text == "gamma"
    # Line ranges are 1-indexed inclusive.
    assert blocks[0].line_start == 1
    assert blocks[0].line_end == 2
    assert blocks[1].line_start == 4
    assert blocks[2].line_start == 7


def test_parser_empty_returns_empty_list() -> None:
    assert parse_plaintext("") == []
    assert parse_plaintext("\n\n\n") == []


def test_parser_slugs_are_stable() -> None:
    text = "# heading\n\nbody of the paragraph\n"
    a = parse_plaintext(text)
    b = parse_plaintext(text)
    assert [blk.slug for blk in a] == [blk.slug for blk in b]


def test_parser_slugs_distinguish_same_first_words() -> None:
    """Two paragraphs that start the same must get different slugs."""
    text = "the fox jumps over\n\nthe fox jumps again\n"
    blocks = parse_plaintext(text)
    assert blocks[0].slug != blocks[1].slug


# ── construction ─────────────────────────────────────────────────────


def test_construction_fails_on_missing_root(store: Store, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        PlaintextHandler(hub=Hub(store=store), root=tmp_path / "no-such-dir")


def test_construction_fails_on_file_root(store: Store, tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("hi")
    with pytest.raises(ValueError, match="not a directory"):
        PlaintextHandler(hub=Hub(store=store), root=f)


# ── index ─────────────────────────────────────────────────────────────


def test_empty_root_lists_no_files(handler: PlaintextHandler) -> None:
    out = handler.get()
    assert "no plaintext files found" in out.body


def test_index_lists_txt_and_log(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "alpha.txt", "hello")
    _write(pt_root, "server.log", "boot line")
    _write(pt_root, "notes/daily.txt", "standup")
    # Non-plaintext files must not leak into the listing.
    _write(pt_root, "notes/ignore.md", "# md")
    out = handler.get()
    assert "3 plaintext file(s)" in out.body
    assert "alpha" in out.body
    assert "server" in out.body
    assert "notes--daily" in out.body
    assert "ignore" not in out.body


# ── overview + block reads ───────────────────────────────────────────


def test_overview_renders_metadata(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(
        pt_root,
        "foo.txt",
        "First paragraph line one.\nFirst paragraph line two.\n\n"
        "Second paragraph here.\n",
    )
    out = handler.get(id="foo")
    assert "foo" in out.body
    assert "paragraphs:  2" in out.body
    assert "path:" in out.body


def test_overview_for_missing_file_raises(handler: PlaintextHandler) -> None:
    with pytest.raises(NotFound, match="not found"):
        handler.get(id="nonexistent")


def test_get_block_by_slug_and_pos(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "doc.txt", "para one here.\n\npara two here.\n")
    # Force ingest via overview.
    handler.get(id="doc")
    out = handler.get(id="doc~0")
    assert "para one" in out.body
    out = handler.get(id="doc~1")
    assert "para two" in out.body


def test_missing_block_raises(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "doc.txt", "single para.\n")
    with pytest.raises(NotFound, match="no paragraph"):
        handler.get(id="doc~nonexistent")
    with pytest.raises(NotFound, match="no paragraph"):
        handler.get(id="doc~99")


# ── view ──────────────────────────────────────────────────────────────


def test_raw_view(handler: PlaintextHandler, pt_root: Path) -> None:
    body = "line one\nline two\n\npara two.\n"
    _write(pt_root, "doc.txt", body)
    out = handler.get(id="doc/raw")
    assert out.body == body


def test_unsupported_view_rejected(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "doc.txt", "hi.\n")
    with pytest.raises(Unsupported, match="unknown plaintext view"):
        handler.get(id="doc/toc")


# ── search ────────────────────────────────────────────────────────────


def test_search_requires_q(handler: PlaintextHandler) -> None:
    with pytest.raises(BadInput, match="search requires q"):
        handler.search()
    with pytest.raises(BadInput, match="search requires q"):
        handler.search(q="   ")


def test_search_empty_corpus_reports_nothing(handler: PlaintextHandler) -> None:
    out = handler.search(q="anything")
    assert "no plaintext blocks match" in out.body


def test_search_finds_lexical_match(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(
        pt_root,
        "notes.txt",
        "Investigated the markdown gating issue.\n\n"
        "Wrapped up at 11am with no regressions.\n",
    )
    # Force ingest.
    handler.get(id="notes")
    out = handler.search(q="markdown gating")
    assert "markdown gating" in out.body.lower() or "notes" in out.body


# ── put: create / append / replace / delete ──────────────────────────


def test_put_create(handler: PlaintextHandler, pt_root: Path) -> None:
    out = handler.put(id="new-file", text="hello world.\n", mode="create")
    assert "created plaintext" in out.body
    assert (pt_root / "new-file.txt").exists()


def test_put_create_refuses_overwrite(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "exists.txt", "already here.\n")
    with pytest.raises(BadInput, match="file already exists"):
        handler.put(id="exists", text="new", mode="create")


def test_put_append(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "foo.txt", "original paragraph.\n")
    out = handler.edit(id="foo", text="new paragraph", mode="append")
    assert "appended to plaintext" in out.body
    content = (pt_root / "foo.txt").read_text()
    assert "original paragraph" in content
    assert "new paragraph" in content


def test_put_append_requires_text(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "foo.txt", "x.\n")
    with pytest.raises(BadInput, match="append requires text"):
        handler.edit(id="foo", mode="append")


def test_put_replace_by_pos(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "doc.txt", "first paragraph.\n\nsecond paragraph.\n")
    # Force ingest, then replace paragraph 0.
    handler.get(id="doc")
    out = handler.edit(id="doc~0", text="FIRST (edited) paragraph.", mode="replace")
    assert "replaced paragraph" in out.body
    content = (pt_root / "doc.txt").read_text()
    assert "FIRST (edited)" in content
    assert "second paragraph" in content


def test_put_delete_by_pos(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "doc.txt", "keep me.\n\ndrop me.\n\nkeep me too.\n")
    handler.get(id="doc")  # force ingest
    out = handler.delete(id="doc~1")
    assert "deleted paragraph" in out.body
    content = (pt_root / "doc.txt").read_text()
    assert "keep me" in content
    assert "drop me" not in content


def test_put_delete_file_without_selector_rejected(
    handler: PlaintextHandler, pt_root: Path
) -> None:
    _write(pt_root, "doc.txt", "x.\n")
    with pytest.raises(BadInput, match="requires a block selector"):
        handler.delete(id="doc")


# ── put: anchored edit + insert (shared protocol) ────────────────────


def test_put_edit_surgical(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(
        pt_root,
        "log.txt",
        "Session opened at 09:15 with tests failing.\n\nResolved at 11:00.\n",
    )
    out = handler.edit(
        id="log",
        mode="find-replace",
        find="09:15",
        text="09:20",
    )
    assert "edited 1 span" in out.body
    content = (pt_root / "log.txt").read_text()
    assert "09:20" in content
    assert "09:15" not in content


def test_put_edit_requires_find(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "log.txt", "x.\n")
    with pytest.raises(BadInput, match="requires find"):
        handler.edit(id="log", mode="find-replace", text="y")


def test_put_insert_before_anchor(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "log.txt", "end of story.\n")
    out = handler.edit(
        id="log",
        mode="insert",
        find="end of story",
        where="before",
        text="PREFIX: ",
    )
    assert "inserted 1 span" in out.body
    assert "PREFIX: end of story" in (pt_root / "log.txt").read_text()


def test_put_edit_dry_run_diff(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "doc.txt", "foo.\n")
    out = handler.put(
        id="doc",
        mode="edit",
        find="foo",
        text="bar",
        dry_run=True,
    )
    # Dry-run must not touch disk.
    assert "foo" in (pt_root / "doc.txt").read_text()
    # The body should be a unified diff.
    assert "---" in out.body or "+++" in out.body or "no diff" in out.body


# ── .log files share the kind ────────────────────────────────────────


def test_log_extension_is_supported(handler: PlaintextHandler, pt_root: Path) -> None:
    _write(pt_root, "server.log", "boot ok\n\nrequest 200\n")
    out = handler.get(id="server")
    assert "server" in out.body
    assert "paragraphs:  2" in out.body
    # Reading /raw returns the .log content verbatim.
    raw = handler.get(id="server/raw")
    assert "boot ok" in raw.body


def test_log_write_preserves_extension(
    handler: PlaintextHandler, pt_root: Path
) -> None:
    """Creating a file always lands as .txt, but editing an existing
    .log file must not rewrite it to .txt on the next write."""
    _write(pt_root, "server.log", "line one.\n")
    handler.get(id="server")  # ingest to pin the .log extension in meta
    handler.edit(id="server", text="line two.", mode="append")
    # Still a .log on disk.
    assert (pt_root / "server.log").exists()
    assert not (pt_root / "server.txt").exists()


# ── path traversal defense ───────────────────────────────────────────


def test_invalid_slug_rejected(handler: PlaintextHandler) -> None:
    with pytest.raises(BadInput, match="invalid plaintext slug"):
        handler.get(id="../escape")
    with pytest.raises(BadInput, match="invalid plaintext slug"):
        handler.get(id="UPPERCASE")
