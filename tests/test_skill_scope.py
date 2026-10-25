"""``utils.skill_scope.render`` + the SkillHandler wiring (gr512233 slice 1).

The write-fence markers (`` {write}`` on H2/H3 headings, own-line
``<!-- write -->`` / ``<!-- /write -->`` pairs, front-matter
``scope: write``) filter a skill body for a read-scope connection.
Markers strip in BOTH scopes — write scope is byte-identical to the
unmarked file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import precis.runtime.scope as scope_mod
from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler
from precis.utils.skill_scope import is_write_call, render
from tests._skill_corpus import _install_corpus


def _gripe_help() -> str:
    """The one marked skill in the shipped corpus."""
    from precis.handlers.skill import _load_skills_map

    return _load_skills_map()["precis-gripe-help"]


MARKED = """---
id: fixture-skill
---
# Fixture

Intro paragraph.

## Read section

search(kind='gripe', q='x')

## Write section {write}

put(kind='gripe', id=1, text='comment')

## Another read section

Tail.
"""


@pytest.fixture(autouse=True)
def _write_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        scope_mod.SERVE_READ_ONLY_ENV,
        "PRECIS_MCP_DENY_VERBS",
        "PRECIS_MCP_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope_mod, "_boot_read_only", False)
    monkeypatch.setattr(scope_mod, "_serve_read_only", False)


@pytest.fixture
def read_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")


@pytest.fixture
def skill() -> SkillHandler:
    return SkillHandler(hub=Hub())


# ── render(): section markers ──────────────────────────────────────


def test_marked_h2_drops_the_body_but_keeps_its_heading() -> None:
    body, dropped = render(MARKED, "read")
    assert "## Write section\n" in body  # the slot stays (~N stability)
    assert "{write}" not in body
    assert "put(kind='gripe', id=1" not in body
    assert "## Read section" in body
    assert "## Another read section" in body
    assert dropped == ["Write section"]
    assert '"Write section"' in body  # the pointer names the cut


def test_h3_marker_drops_to_same_or_higher_only() -> None:
    text = (
        "# T\n"
        "## Outer\n\n"
        "intro\n\n"
        "### Inner {write}\n\n"
        "#### Deep\n\n"
        "secret write recipe\n\n"
        "### Sibling\n\n"
        "kept\n\n"
        "## Next\n\n"
        "tail\n"
    )
    body, dropped = render(text, "read")
    assert "secret write recipe" not in body
    assert "#### Deep" not in body  # deeper heading dies with the marked H3
    assert "### Sibling" in body
    assert "## Next" in body
    assert dropped == ["Inner"]


def test_alias_group_marker_on_any_heading_marks_the_group() -> None:
    text = (
        "# T\n\n"
        "## Read\n\n"
        "x\n\n"
        "## Alias one\n"
        "## Alias two {write}\n"
        "## Alias three\n\n"
        "shared write body\n\n"
        "## After\n\n"
        "tail\n"
    )
    body, dropped = render(text, "read")
    assert "shared write body" not in body
    # The whole stacked group is one chunk; its headings stay so the
    # ``~N`` slot survives, and the pointer names the first heading.
    assert "## Alias one\n" in body
    assert "## Alias two\n" in body
    assert "## Alias three\n" in body
    assert '"Alias one" needs a write connection' in body
    assert "## After" in body
    assert dropped == ["Alias one"]


def test_write_scope_strips_section_marker_and_keeps_body() -> None:
    body, dropped = render(MARKED, "write")
    assert dropped == []
    assert "{write}" not in body
    assert "put(kind='gripe', id=1" in body  # content kept
    assert "## Write section\n" in body


# ── render(): inline fences ─────────────────────────────────────────


def test_inline_fence_drops_lines_and_names_enclosing_heading() -> None:
    text = (
        "# T\n\n"
        "## Mixed\n\n"
        "read lines\n\n"
        "<!-- write -->\n"
        "tag(kind='gripe', id=1, add=['x'])\n"
        "<!-- /write -->\n\n"
        "more read\n"
    )
    body, dropped = render(text, "read")
    assert "tag(kind='gripe'" not in body
    assert "more read" in body
    assert dropped == ["Mixed"]
    assert '"Mixed"' in body


def test_inline_fence_in_write_scope_keeps_content() -> None:
    text = "a\n<!-- write -->\nb\n<!-- /write -->\nc\n"
    body, dropped = render(text, "write")
    assert body == "a\nb\nc\n"  # byte-identical to the unmarked file
    assert dropped == []


def test_unbalanced_close_raises() -> None:
    with pytest.raises(ValueError, match="without"):
        render("a\n<!-- /write -->\n", "write")


def test_nested_open_raises() -> None:
    text = "<!-- write -->\n<!-- write -->\n<!-- /write -->\n"
    with pytest.raises(ValueError, match="nested"):
        render(text, "write")


def test_unclosed_open_raises() -> None:
    with pytest.raises(ValueError, match="unclosed"):
        render("a\n<!-- write -->\nb\n", "write")


# ── render(): code fences hide fake headings ───────────────────────


def test_code_comment_is_not_a_heading_and_does_not_end_a_drop() -> None:
    """``#``-led lines inside a ``` fence are code, not headings: a
    ``# comment`` inside a marked section's example block must not
    truncate the drop (the section's remaining write lines would leak),
    and must not hijack the pointer's heading name."""
    text = (
        "# T\n\n"
        "## Write section {write}\n\n"
        "```python\n"
        "put(kind='x', id=1)\n"
        "# → created id=1\n"
        "tag(kind='x', id=1)\n"
        "```\n\n"
        "still write content\n\n"
        "## After\n\n"
        "tail\n"
    )
    body, dropped = render(text, "read")
    assert "tag(kind='x'" not in body  # the drop runs to ## After
    assert "still write content" not in body
    assert "## After" in body
    assert dropped == ["Write section"]
    assert '"Write section"' in body


def test_code_comment_does_not_name_an_inline_fence_pointer() -> None:
    text = (
        "# T\n\n"
        "## Mixed\n\n"
        "```python\n"
        "x = 1\n"
        "# → done\n"
        "```\n\n"
        "<!-- write -->\n"
        "tag(kind='x', id=1)\n"
        "<!-- /write -->\n"
    )
    body, dropped = render(text, "read")
    assert dropped == ["Mixed"]
    assert '"Mixed"' in body


def test_markers_inside_a_code_fence_still_count() -> None:
    """The marker lines are honoured inside a ``` block — a stray fenced
    line in an example drops as authored (slice-5 lint owns the case of
    a genuinely-literal marker string)."""
    text = (
        "# T\n\n"
        "## S\n\n"
        "```python\n"
        "create()\n"
        "<!-- write -->\n"
        "mutate()\n"
        "<!-- /write -->\n"
        "```\n"
    )
    body, dropped = render(text, "read")
    assert "create()" in body
    assert "mutate()" not in body
    assert dropped == ["S"]


def test_chunk_slots_are_stable_across_scopes() -> None:
    """``slug~N`` names the same section in both scopes (spec table:
    a fully dropped chunk serves the pointer, it doesn't vanish). On
    precis-gripe-help the read render must keep every H2 chunk and
    heading the write render has."""
    from precis.skill_index.chunker import chunk_by_h2

    text = _gripe_help()
    write_chunks = chunk_by_h2(render(text, "write")[0])
    read_chunks = chunk_by_h2(render(text, "read")[0])
    assert [c.heading for c in read_chunks] == [c.heading for c in write_chunks]


# ── corpus guard: every shipped skill renders ─────────────────────

_SKILLS_DIR = Path(__file__).parent.parent / "src" / "precis" / "data" / "skills"
_SKILL_FILES = sorted(_SKILLS_DIR.rglob("*.md"))


@pytest.mark.parametrize(
    "path", _SKILL_FILES, ids=[str(p.relative_to(_SKILLS_DIR)) for p in _SKILL_FILES]
)
def test_every_skill_renders_in_both_scopes(path: Path) -> None:
    """gr512233 round-2 corpus guard: ``render`` must not raise on ANY
    shipped skill — a malformed ``<!-- /write -->`` would 500 every
    serve of that file, writers included. An unmarked file renders
    byte-identical in both scopes; a marked one's write render holds
    no marker remnants."""
    text = path.read_text(encoding="utf-8")
    write, _ = render(text, "write")
    read, _ = render(text, "read")
    assert "{write}" not in write
    assert "<!-- write -->" not in write
    assert "<!-- /write -->" not in write
    if "{write}" not in text and "<!-- write -->" not in text:
        assert write == text
        assert read == text


# ── is_write_call: the gripe-create exemption ───────────────────────


def test_gripe_create_is_not_a_write_call() -> None:
    assert is_write_call("put(kind='gripe', text='x')") is False
    assert is_write_call('put(kind="gripe", text="x")') is False
    # Multi-line shape skills use.
    assert is_write_call('put(\n    kind="gripe",\n    text="x",\n)') is False


def test_gripe_put_with_write_kwargs_is_a_write_call() -> None:
    assert is_write_call("put(kind='gripe', id=42, text='x')") is True  # comment
    assert is_write_call("put(kind='gripe', text='x', link='skill:y')") is True
    assert is_write_call("put(kind='gripe', text='x', tags=['t'])") is True


def test_other_verbs_and_kinds_are_write_calls() -> None:
    assert is_write_call("put(kind='memory', text='x')") is True
    assert is_write_call("edit(kind='gripe', id=1, text='x')") is True
    assert is_write_call("delete(kind='gripe', id=1)") is True
    assert is_write_call("tag(kind='gripe', id=1, add=['a'])") is True
    assert is_write_call("link(kind='gripe', id=1, target='gripe:2')") is True


def test_read_verbs_are_not_write_calls() -> None:
    assert is_write_call("get(kind='gripe', id=1)") is False
    assert is_write_call("search(kind='gripe', q='x')") is False


# ── markers survive chunk_by_h2 and the md indexer ──────────────────


def test_markers_survive_chunk_by_h2() -> None:
    """The marker travels in the raw text — ``chunk_by_h2`` parses a
    marked file unchanged, so the embedding index (built on the
    write-scope body) keeps working."""
    from precis.skill_index.chunker import chunk_by_h2

    chunks = chunk_by_h2(MARKED)
    headings = [c.heading for c in chunks]
    assert "Write section {write}" in headings


def test_markers_survive_the_md_indexer(tmp_path) -> None:
    """``index_file`` (the ``md`` kind's indexer) parses markers without
    mangling: the ``{write}`` suffix rides along in the heading title
    and the comment pair is ordinary paragraph content."""
    from precis.md_index.indexer import index_file

    p = tmp_path / "marked.md"
    p.write_text(MARKED, encoding="utf-8")
    entry = index_file(p, file_relative="marked.md")
    assert entry.n_blocks > 0
    titles = [b.title for b in entry.blocks if b.title]
    assert any(t == "Write section {write}" for t in titles)


# ── SkillHandler.get wiring ─────────────────────────────────────────


def _serve(handler: SkillHandler, slug: str, **kw: Any) -> str:
    return handler.get(id=slug, **kw).body


def test_read_scope_filters_body_and_inserts_pointers(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"fixture": MARKED})
    body = _serve(skill, "fixture")
    assert "put(kind='gripe', id=1" not in body
    assert "write recipe omitted" in body
    assert '"Write section"' in body
    assert "## Another read section" in body


def test_write_scope_serve_is_byte_identical_to_unmarked(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    unmarked = MARKED.replace("## Write section {write}", "## Write section")
    _install_corpus(monkeypatch, {"fixture": MARKED})
    marked_serve = _serve(skill, "fixture")
    _install_corpus(monkeypatch, {"fixture": unmarked})
    unmarked_serve = _serve(skill, "fixture")
    assert marked_serve == unmarked_serve


def test_scope_gives_different_revs_and_no_cross_stub(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"fixture": MARKED})
    import re

    read_body = _serve(skill, "fixture")
    read_m = re.search(r"skill rev (\w+)", read_body)
    assert read_m is not None
    read_rev = read_m.group(1)
    monkeypatch.delenv("PRECIS_MCP_DENY_VERBS")
    write_body = _serve(skill, "fixture")
    write_m = re.search(r"skill rev (\w+)", write_body)
    assert write_m is not None
    write_rev = write_m.group(1)
    assert read_rev != write_rev
    # A have= minted in one scope must not stub the other.
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    assert "skill rev" in _serve(skill, "fixture", have=write_rev)
    stub = _serve(skill, "fixture", have=read_rev)
    assert "unchanged since you read it" in stub


def test_chunk_and_toc_paths_filter(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"fixture": MARKED})
    chunks_body = _serve(skill, "fixture~0..2")
    assert "put(kind='gripe', id=1" not in chunks_body
    assert "write recipe omitted" in chunks_body
    toc_body = _serve(skill, "fixture/toc")
    assert "put(kind='gripe', id=1" not in toc_body


def test_scope_write_skill_serves_a_stub(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_only = (
        "---\nid: w-skill\ntitle: W\nsummary: write things\nscope: write\n---\n"
        "# W\n\nput(kind='x', text='y')\n"
    )
    _install_corpus(monkeypatch, {"w-skill": write_only})
    body = _serve(skill, "w-skill")
    assert "put(kind='x'" not in body
    assert "write recipes omitted" in body
    assert "summary: write things" in body
    # ~N and /toc take the same stub.
    assert "put(kind='x'" not in _serve(skill, "w-skill~0")
    assert "put(kind='x'" not in _serve(skill, "w-skill/toc")
    # Write scope serves the real body.
    monkeypatch.delenv("PRECIS_MCP_DENY_VERBS")
    assert "put(kind='x'" in _serve(skill, "w-skill")


def test_toc_cache_is_scoped(
    skill: SkillHandler, read_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A toc cached in one scope must not serve in the other (the key
    gained the connection scope)."""
    _install_corpus(monkeypatch, {"fixture": MARKED})
    read_toc = _serve(skill, "fixture/toc")
    monkeypatch.delenv("PRECIS_MCP_DENY_VERBS")
    write_toc = _serve(skill, "fixture/toc")
    assert read_toc != write_toc
    assert "Write section" in write_toc
    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    assert _serve(skill, "fixture/toc") == read_toc  # cache hit, not a re-render
