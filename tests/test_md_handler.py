"""Tests for `precis.handlers.md.MdHandler`.

DB-free — the md kind is in-memory by design (mirrors
`tests/test_python_handler.py`). Each test stands up a tiny markdown
tree in `tmp_path`, constructs the handler directly (bypassing
`boot()`/env parsing — that's covered by `test_md_config_wire.py`),
and exercises get/search plus the read-only-verb contract.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.md import MdHandler, _parse_id
from precis.md_index import MdVectorCache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(repo: Path, rel: str, content: str) -> Path:
    file = repo / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small markdown tree: two files, nested headings."""
    _write(
        tmp_path,
        "docs/guide.md",
        """
        # Guide

        Intro paragraph about the guide.

        ## Ship workflow

        How to make search fresh after shipping: run the deploy script.

        ### Sub-section

        Nested detail under ship workflow.

        ## Other section

        Unrelated prose here.
        """,
    )
    _write(
        tmp_path,
        "readme.md",
        """
        # Readme

        Top-level readme paragraph.
        """,
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> MdHandler:
    return MdHandler(hub=Hub(), roots={"r": repo})


class _StubEmbedder:
    """Deterministic embedder stub structurally matching
    `precis.embedder.Embedder` (the `Hub.embedder` field's type)."""

    model = "stub-embedder"
    dim = 3

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [1.0, 0.0, 0.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return list(self.vector)

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def unload(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construct_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        MdHandler(hub=Hub(), roots={"r": tmp_path / "no-such-dir"})


def test_construct_rejects_invalid_alias(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid md root alias"):
        MdHandler(hub=Hub(), roots={"bad/alias": tmp_path})


def test_construct_resolves_paths(tmp_path: Path) -> None:
    handler = MdHandler(hub=Hub(), roots={"r": tmp_path})
    assert handler.roots["r"] == tmp_path.resolve()


def test_construct_with_storeless_hub_no_embedder(repo: Path) -> None:
    """Handler works with a store-less hub — zero DB connections. ``Hub()``
    defaults ``store=None``, so this is already the store-less shape."""
    handler = MdHandler(hub=Hub(), roots={"r": repo})
    assert handler.embedder is None
    assert handler.vector_cache is None


def test_construct_with_storeless_hub_and_embedder_builds_vector_cache(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    handler = MdHandler(hub=Hub(embedder=_StubEmbedder()), roots={"r": repo})
    assert handler.vector_cache is not None
    assert handler.vector_cache.model == "stub-embedder"


# ---------------------------------------------------------------------------
# Address parser
# ---------------------------------------------------------------------------


def test_parse_id_alias_only() -> None:
    p = _parse_id("r")
    assert p.alias == "r"
    assert p.file is None
    assert p.selector is None


def test_parse_id_alias_and_file() -> None:
    p = _parse_id("r/docs/guide.md")
    assert p.alias == "r"
    assert p.file == "docs/guide.md"
    assert p.selector is None


def test_parse_id_with_selector() -> None:
    p = _parse_id("r/docs/guide.md~ship-workflow")
    assert p.file == "docs/guide.md"
    assert p.selector == "ship-workflow"


def test_parse_id_empty_raises() -> None:
    with pytest.raises(BadInput):
        _parse_id("")


def test_parse_id_missing_alias_before_slash_raises() -> None:
    with pytest.raises(BadInput, match="missing alias"):
        _parse_id("/docs/guide.md")


def test_parse_id_selector_without_file_raises() -> None:
    with pytest.raises(BadInput, match="requires a file"):
        _parse_id("r~heading")


def test_parse_id_empty_selector_raises() -> None:
    with pytest.raises(BadInput, match="empty selector"):
        _parse_id("r/docs/guide.md~")


# ---------------------------------------------------------------------------
# get — index / overview / file / section views
# ---------------------------------------------------------------------------


def test_get_no_id_lists_roots(handler: MdHandler) -> None:
    resp = handler.get()
    assert "md - 1 root" in resp.body
    assert "r" in resp.body
    assert "Next:" in resp.body


def test_get_no_id_empty_roots() -> None:
    handler = MdHandler(hub=Hub(), roots={})
    resp = handler.get()
    assert "no roots configured" in resp.body
    assert "PRECIS_MD_ROOTS" in resp.body


def test_get_unknown_alias_raises_not_found(handler: MdHandler) -> None:
    with pytest.raises(NotFound, match="unknown md root alias"):
        handler.get(id="nope")


def test_get_alias_overview(handler: MdHandler) -> None:
    resp = handler.get(id="r")
    assert "md root overview" in resp.body
    assert "Files:  2" in resp.body
    assert "docs" in resp.body  # top-level dir


def test_overview_hint_file_is_verifiable_on_the_page(handler: MdHandler) -> None:
    """hint-audit item 7: the overview's drill-down hint names
    ``idx.files[0]`` (here ``docs/guide.md``), a file the overview body
    never otherwise listed (only top-level DIR counts) — the reader
    couldn't verify the hint's target from what's on the page. The
    fixed overview prints the sample filename right next to the hint."""
    resp = handler.get(id="r")
    assert "docs/guide.md" in resp.body
    assert "Sample file: docs/guide.md" in resp.body
    assert "get(kind='md', id='r/docs/guide.md')" in resp.body
    # And it actually dispatches.
    handler.get(id="r/docs/guide.md")


def test_get_overview_view_source_requires_file(handler: MdHandler) -> None:
    with pytest.raises(BadInput, match="requires a file"):
        handler.get(id="r", view="source")


def test_get_unknown_view_raises_unsupported(handler: MdHandler) -> None:
    with pytest.raises(Unsupported, match="unknown md view"):
        handler.get(id="r/docs/guide.md", view="bogus")


def test_get_file_outline(handler: MdHandler) -> None:
    resp = handler.get(id="r/docs/guide.md")
    assert "r/docs/guide.md" in resp.body
    assert "Guide" in resp.body
    assert "Ship workflow" in resp.body
    assert "Sub-section" in resp.body
    assert "Next:" in resp.body


def test_get_missing_file_raises_not_found(handler: MdHandler) -> None:
    with pytest.raises(NotFound, match="not found"):
        handler.get(id="r/docs/nope.md")


def test_get_file_source_view_returns_full_text(handler: MdHandler) -> None:
    resp = handler.get(id="r/readme.md", view="source")
    assert "# Readme" in resp.body
    assert "Top-level readme paragraph." in resp.body


def test_get_section_by_slug(handler: MdHandler) -> None:
    outline = handler.get(id="r/docs/guide.md")
    # pull the minted slug for "Ship workflow" out of the outline body
    slug_line = next(ln for ln in outline.body.splitlines() if "Ship workflow" in ln)
    slug = slug_line.split("(~", 1)[1].split(",", 1)[0]

    resp = handler.get(id=f"r/docs/guide.md~{slug}")
    assert "Ship workflow" in resp.body
    assert "fresh after shipping" in resp.body
    # nested sub-section content is included
    assert "Nested detail" in resp.body
    # sibling section is not
    assert "Unrelated prose" not in resp.body


def test_get_section_by_title_case_insensitive(handler: MdHandler) -> None:
    resp = handler.get(id="r/docs/guide.md~ship workflow")
    assert "fresh after shipping" in resp.body


def test_get_section_unknown_selector_raises_not_found(handler: MdHandler) -> None:
    with pytest.raises(NotFound, match="no heading"):
        handler.get(id="r/docs/guide.md~does-not-exist")


# ---------------------------------------------------------------------------
# search — lexical only (no embedder)
# ---------------------------------------------------------------------------


def test_search_requires_q(handler: MdHandler) -> None:
    with pytest.raises(BadInput, match="requires q"):
        handler.search(q=None)
    with pytest.raises(BadInput, match="requires q"):
        handler.search(q="   ")


def test_search_lexical_only_finds_hit(handler: MdHandler) -> None:
    resp = handler.search(q="ship workflow")
    assert "md hit" in resp.body
    assert "lexical only: no embedder wired" in resp.body
    assert "docs/guide.md" in resp.body


def test_search_no_hits_returns_recovery_body(handler: MdHandler) -> None:
    resp = handler.search(q="nonexistent-term-xyz")
    assert "no md hits" in resp.body
    assert "Next:" in resp.body


def test_search_unknown_scope_raises_not_found(handler: MdHandler) -> None:
    with pytest.raises(NotFound, match="no md root matches"):
        handler.search(q="guide", scope="nope")


def test_search_scope_restricts_to_file(handler: MdHandler) -> None:
    resp = handler.search(q="readme", scope="r/readme.md")
    assert "readme.md" in resp.body
    assert "guide.md" not in resp.body


def test_search_scope_restricts_to_subdir(handler: MdHandler) -> None:
    resp = handler.search(q="ship workflow", scope="r/docs")
    assert "guide.md" in resp.body


# ---------------------------------------------------------------------------
# search — hybrid (stub embedder + prefilled vector cache)
# ---------------------------------------------------------------------------


def test_search_hybrid_uses_semantic_leg_when_vectors_cached(
    repo: Path, tmp_path: Path
) -> None:
    embedder = _StubEmbedder(vector=[1.0, 0.0, 0.0])
    vector_cache = MdVectorCache(
        model=embedder.model, dim=embedder.dim, cache_dir=tmp_path
    )
    handler = MdHandler(
        hub=Hub(embedder=embedder),
        roots={"r": repo},
        vector_cache=vector_cache,
    )

    # Pre-embed every block so the semantic leg has full coverage.
    idx = handler.cache.get(repo)
    for _, block in idx.all_blocks():
        vector_cache.add(block.sha256, embedder.vector)

    resp = handler.search(q="ship workflow")
    assert "lexical only" not in resp.body
    assert "semantic:" not in resp.body  # fully indexed -> no coverage caveat
    assert "docs/guide.md" in resp.body


def test_search_hybrid_reports_partial_coverage(repo: Path, tmp_path: Path) -> None:
    embedder = _StubEmbedder()
    vector_cache = MdVectorCache(
        model=embedder.model, dim=embedder.dim, cache_dir=tmp_path
    )
    handler = MdHandler(
        hub=Hub(embedder=embedder),
        roots={"r": repo},
        vector_cache=vector_cache,
    )
    # Deliberately leave the cache cold (0 of N blocks embedded).
    resp = handler.search(q="ship workflow")
    assert "semantic: 0% of blocks indexed" in resp.body


def test_search_hybrid_embedder_raises_falls_back_to_lexical(
    repo: Path, tmp_path: Path
) -> None:
    """embed_query degrades gracefully when the embedder itself raises."""

    class _BrokenEmbedder(_StubEmbedder):
        def embed_one(self, text: str) -> list[float]:
            raise RuntimeError("embedder down")

    embedder = _BrokenEmbedder()
    vector_cache = MdVectorCache(
        model=embedder.model, dim=embedder.dim, cache_dir=tmp_path
    )
    handler = MdHandler(
        hub=Hub(embedder=embedder),
        roots={"r": repo},
        vector_cache=vector_cache,
    )
    resp = handler.search(q="ship workflow")
    assert "docs/guide.md" in resp.body


# ---------------------------------------------------------------------------
# Read-only contract: put/edit/delete/tag/link raise Unsupported
# ---------------------------------------------------------------------------


def test_put_unsupported(handler: MdHandler) -> None:
    with pytest.raises(Unsupported, match="does not support put"):
        handler.put(id="r/new.md", text="hi")


def test_edit_unsupported(handler: MdHandler) -> None:
    with pytest.raises(Unsupported, match="does not support edit"):
        handler.edit(id="r/readme.md", text="hi")


def test_delete_unsupported(handler: MdHandler) -> None:
    with pytest.raises(Unsupported, match="does not support delete"):
        handler.delete(id="r/readme.md")


def test_tag_unsupported(handler: MdHandler) -> None:
    with pytest.raises(Unsupported, match="does not support tag"):
        handler.tag(id="r/readme.md", add=["x"])


def test_link_unsupported(handler: MdHandler) -> None:
    with pytest.raises(Unsupported, match="does not support link"):
        handler.link(id="r/readme.md", target="r/guide.md")


def test_kindspec_declares_only_get_and_search() -> None:
    spec = MdHandler.spec
    assert spec.supports_get is True
    assert spec.supports_search is True
    assert spec.supports_put is False
    assert spec.supports_edit is False
    assert spec.supports_delete is False
    assert spec.supports_tag is False
    assert spec.supports_link is False
