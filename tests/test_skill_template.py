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


def test_expand_substitutes_with_markers() -> None:
    text = "before {{include doc:precis-common#x}} after"
    includer = Includer(
        resolvers={"doc": _stub({"precis-common#x": "RESOLVED BODY"})}
    )
    out = includer.expand(text)
    assert "RESOLVED BODY" in out
    assert "<!-- inlined-from: doc:precis-common#x -->" in out
    assert "<!-- /inlined-from doc:precis-common#x -->" in out
    assert "before " in out and " after" in out


def test_expand_no_directives_is_identity() -> None:
    text = "## just a heading\n\nbody text\n"
    includer = Includer(resolvers={})
    assert includer.expand(text) == text


def test_expand_multiple_directives() -> None:
    text = (
        "intro {{include doc:a#one}} middle {{include doc:b}}\n"
    )
    includer = Includer(
        resolvers={"doc": _stub({"a#one": "AAA", "b": "BBB"})}
    )
    out = includer.expand(text)
    assert "AAA" in out
    assert "BBB" in out
    # First-directive substitution must not corrupt the second-directive
    # span — both markers present.
    assert out.count("<!-- inlined-from:") == 2


def test_expand_no_resolver_raises() -> None:
    text = "{{include schema:put#arguments}}"
    includer = Includer(resolvers={"doc": lambda s, sec: ""})
    with pytest.raises(IncludeError, match="no resolver registered for source 'schema'"):
        includer.expand(text)


def test_expand_resolver_failure_raises() -> None:
    text = "{{include doc:missing#x}}"
    includer = Includer(resolvers={"doc": _stub({})})
    with pytest.raises(IncludeError, match="missing#x"):
        includer.expand(text)


# ── slugify ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("Address grammar", "address-grammar"),
    ("Find a paper by topic", "find-a-paper-by-topic"),
    ("Arguments to the `put` verb", "arguments-to-the-put-verb"),
    ("  Whitespace at ends   ", "whitespace-at-ends"),
    ("Mixed CASE and DASH-words", "mixed-case-and-dash-words"),
])
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
    body = (
        "## First\n"
        "first body\n"
        "more first body\n"
        "## Second\n"
        "second body\n"
    )
    r = DocResolver(docs={"d": body})
    out = r("d", "first")
    assert "first body" in out
    assert "more first body" in out
    assert "second body" not in out


def test_docresolver_section_terminates_at_next_h1() -> None:
    body = (
        "## First\n"
        "first body\n"
        "# Big break\n"
        "after\n"
    )
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
    includer = Includer(resolvers={"doc": DocResolver(docs={"precis-common": precis_common})})
    out = includer.expand(skill)
    assert "Use `slug~N`" in out
    assert "UPPERCASE replaces" not in out
    assert "<!-- inlined-from: doc:precis-common#address-grammar -->" in out


def test_directive_span_round_trip() -> None:
    # Sanity check that the span captures the directive precisely.
    text = "X {{include doc:a#b}} Y"
    [d] = parse_directives(text)
    assert text[d.span[0]:d.span[1]] == "{{include doc:a#b}}"
    assert isinstance(d, IncludeDirective)
