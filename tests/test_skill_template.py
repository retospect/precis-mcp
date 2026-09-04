"""Tests for the template-include preprocessor.

Covers directive parsing, expansion with stub resolvers, the
built-in :class:`DocResolver`, slugify behaviour, and the HTML
comment markers that wrap substitutions.
"""

from __future__ import annotations

import pytest

from precis.ingest.skill_template import (
    DocResolver,
    IncludeDirective,
    IncludeError,
    Includer,
    parse_directives,
    slugify_heading,
)

# ── directive parsing ────────────────────────────────────────────────


def test_parse_no_directives() -> None:
    assert parse_directives("just some markdown\n## heading\nbody") == []


def test_parse_single_directive_with_section() -> None:
    text = "before {{include doc:precis-common#address-grammar}} after"
    ds = parse_directives(text)
    assert len(ds) == 1
    d = ds[0]
    assert d.source == "doc"
    assert d.slug == "precis-common"
    assert d.section == "address-grammar"
    assert d.label() == "doc:precis-common#address-grammar"


def test_parse_directive_without_section() -> None:
    ds = parse_directives("{{include doc:precis-common}}")
    assert len(ds) == 1
    assert ds[0].section is None
    assert ds[0].label() == "doc:precis-common"


def test_parse_schema_directive() -> None:
    ds = parse_directives("{{include schema:put#arguments}}")
    assert len(ds) == 1
    assert ds[0].source == "schema"
    assert ds[0].slug == "put"
    assert ds[0].section == "arguments"


def test_parse_multiple_directives_in_order() -> None:
    text = (
        "intro {{include doc:a#one}} middle "
        "{{include schema:put#arguments}} end\n"
        "{{include doc:b}}"
    )
    ds = parse_directives(text)
    labels = [d.label() for d in ds]
    assert labels == [
        "doc:a#one",
        "schema:put#arguments",
        "doc:b",
    ]


def test_parse_tolerates_whitespace_in_directive() -> None:
    # ``include   doc:a#one  `` — extra spaces shouldn't trip the regex.
    text = "{{include   doc:a#one   }}"
    ds = parse_directives(text)
    assert ds and ds[0].label() == "doc:a#one"


# ── expansion ────────────────────────────────────────────────────────


def _stub(label_to_body: dict[str, str]):
    """Return a resolver that maps ``slug[#section]`` → body."""

    def resolve(slug: str, section: str | None) -> str:
        key = f"{slug}#{section}" if section else slug
        if key not in label_to_body:
            raise IncludeError(f"stub: {key!r} not found")
        return label_to_body[key]

    return resolve


def test_expand_substitutes_inline() -> None:
    text = "before {{include doc:precis-common#x}} after"
    includer = Includer(resolvers={"doc": _stub({"precis-common#x": "RESOLVED BODY"})})
    out = includer.expand(text)
    assert "RESOLVED BODY" in out
    assert "{{include" not in out
    # Substitution is verbatim — no HTML-comment markers (keeps
    # MCP-served bodies free of low-signal tokens).
    assert "<!-- inlined-from" not in out
    assert "before " in out and " after" in out


def test_expand_no_directives_is_identity() -> None:
    text = "## just a heading\n\nbody text\n"
    includer = Includer(resolvers={})
    assert includer.expand(text) == text


def test_expand_multiple_directives() -> None:
    text = "intro {{include doc:a#one}} middle {{include doc:b}}\n"
    includer = Includer(resolvers={"doc": _stub({"a#one": "AAA", "b": "BBB"})})
    out = includer.expand(text)
    assert "AAA" in out
    assert "BBB" in out
    # Both directives substituted — no raw directive tokens remain.
    assert "{{include" not in out
    # Order preserved: AAA's substitution appears before BBB's,
    # matching the source span order.
    assert out.index("AAA") < out.index("BBB")


def test_expand_no_resolver_raises() -> None:
    text = "{{include schema:put#arguments}}"
    includer = Includer(resolvers={"doc": lambda s, sec: ""})
    with pytest.raises(
        IncludeError, match="no resolver registered for source 'schema'"
    ):
        includer.expand(text)


def test_expand_resolver_failure_raises() -> None:
    text = "{{include doc:missing#x}}"
    includer = Includer(resolvers={"doc": _stub({})})
    with pytest.raises(IncludeError, match="missing#x"):
        includer.expand(text)


# ── slugify ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Address grammar", "address-grammar"),
        ("Find a paper by topic", "find-a-paper-by-topic"),
        ("Arguments to the `put` verb", "arguments-to-the-put-verb"),
        ("  Whitespace at ends   ", "whitespace-at-ends"),
        ("Mixed CASE and DASH-words", "mixed-case-and-dash-words"),
    ],
)
def test_slugify_heading(text: str, expected: str) -> None:
    assert slugify_heading(text) == expected


# ── DocResolver ──────────────────────────────────────────────────────


def test_docresolver_returns_whole_body_when_no_section() -> None:
    body = "# Title\n\nbody text\n"
    r = DocResolver(docs={"foo": body})
    assert r("foo", None) == body


def test_docresolver_strips_frontmatter() -> None:
    body = "---\nid: foo\nstatus: active\n---\n# Title\n\nbody text\n"
    r = DocResolver(docs={"foo": body})
    out = r("foo", None)
    assert "id: foo" not in out
    assert "body text" in out


def test_docresolver_unknown_slug_raises() -> None:
    r = DocResolver(docs={})
    with pytest.raises(IncludeError, match="unknown slug 'nope'"):
        r("nope", None)


def test_docresolver_extracts_named_section() -> None:
    body = (
        "## Address grammar\n"
        "Use `slug~N` for chunk N.\n"
        "\n"
        "## Tag semantics\n"
        "UPPERCASE replaces, lowercase accumulates.\n"
    )
    r = DocResolver(docs={"common": body})
    out = r("common", "address-grammar")
    assert "Use `slug~N`" in out
    assert "UPPERCASE replaces" not in out


def test_docresolver_section_not_found_raises() -> None:
    body = "## Address grammar\nbody\n"
    r = DocResolver(docs={"common": body})
    with pytest.raises(IncludeError, match="section 'tag-semantics' not found"):
        r("common", "tag-semantics")


def test_docresolver_section_terminates_at_next_h2() -> None:
    body = "## First\nfirst body\nmore first body\n## Second\nsecond body\n"
    r = DocResolver(docs={"d": body})
    out = r("d", "first")
    assert "first body" in out
    assert "more first body" in out
    assert "second body" not in out


def test_docresolver_section_terminates_at_next_h1() -> None:
    body = "## First\nfirst body\n# Big break\nafter\n"
    r = DocResolver(docs={"d": body})
    out = r("d", "first")
    assert "first body" in out
    assert "after" not in out


# ── end-to-end with DocResolver ──────────────────────────────────────


def test_includer_with_docresolver_e2e() -> None:
    precis_common = (
        "---\nid: precis-common\n---\n"
        "## Address grammar\n"
        "Use `slug~N`.\n"
        "## Tag semantics\n"
        "UPPERCASE replaces.\n"
    )
    skill = (
        "# precis-search-help\n\n"
        "Search lets you find content.\n\n"
        "{{include doc:precis-common#address-grammar}}\n\n"
        "More skill text.\n"
    )
    includer = Includer(
        resolvers={"doc": DocResolver(docs={"precis-common": precis_common})}
    )
    out = includer.expand(skill)
    assert "Use `slug~N`" in out
    assert "UPPERCASE replaces" not in out
    assert "{{include" not in out


# ── fence-awareness (gr311346) ──────────────────────────────────────


def test_parse_directives_skips_fenced_code_block() -> None:
    text = (
        "before\n"
        "```\n"
        "{{include doc:a#b}}\n"
        "```\n"
        "after {{include doc:c#d}}\n"
    )
    ds = parse_directives(text)
    assert [d.label() for d in ds] == ["doc:c#d"]


def test_parse_directives_skips_fenced_block_with_info_string() -> None:
    # Fences carrying a language info string (```text, ```python, …)
    # are just as much a fence as a bare ```.
    text = "```text\n{{include doc:a#b}}\n```\n"
    assert parse_directives(text) == []


def test_parse_directives_skips_tilde_fence() -> None:
    text = "~~~\n{{include doc:a#b}}\n~~~\n"
    assert parse_directives(text) == []


def test_parse_directives_skips_inline_code_span() -> None:
    text = "Use `{{include doc:a#b}}` to pull in a section."
    assert parse_directives(text) == []


def test_parse_directives_unterminated_fence_excludes_rest() -> None:
    # A broken/unterminated fence is treated as fenced through
    # end-of-text — better to under-expand than to splice mid-fence.
    text = "```\n{{include doc:a#b}}\n"
    assert parse_directives(text) == []


def test_expand_leaves_fenced_directive_untouched() -> None:
    text = "```\n{{include doc:a#b}}\n```\n{{include doc:a#b}}\n"
    includer = Includer(resolvers={"doc": _stub({"a#b": "RESOLVED"})})
    out = includer.expand(text)
    # The fenced occurrence survives verbatim; the live one outside
    # the fence expands.
    assert out.count("{{include doc:a#b}}") == 1
    assert "RESOLVED" in out


def test_self_referential_fenced_example_is_not_expanded() -> None:
    """Regression for gr311346: a doc that teaches the ``{{include}}``
    syntax inside a fenced example, where the shown directive happens
    to resolve against a section of the very same doc, must not
    self-expand and duplicate that section."""
    body = (
        "Example:\n"
        "```\n"
        "{{include doc:self#foo}}\n"
        "```\n"
        "## Foo\n"
        "foo body\n"
    )
    includer = Includer(resolvers={"doc": DocResolver(docs={"self": body})})
    out = includer.expand(body)
    assert out == body
    assert out.count("## Foo") == 1


def test_directive_span_round_trip() -> None:
    # Sanity check that the span captures the directive precisely.
    text = "X {{include doc:a#b}} Y"
    [d] = parse_directives(text)
    assert text[d.span[0] : d.span[1]] == "{{include doc:a#b}}"
    assert isinstance(d, IncludeDirective)
