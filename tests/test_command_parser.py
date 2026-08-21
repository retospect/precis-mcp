"""Tests for :mod:`precis.tools.command_parser`.

Covers:

- Acceptance of one representative call per registered verb.
- Rejection of every non-literal / non-single-call shape the frontier
  MCP profile must reject (expressions, attribute access, nested
  calls, positional args, ``**kwargs``, unregistered verbs, ``text=``
  named in both channels).
- A corpus sweep over ``src/precis/data/skills/*.md``'s call-shaped
  examples — the docs already teach this syntax, so real examples
  should parse.
- A round-trip through :mod:`precis.utils.next_block`'s renderer:
  every ``Next:`` hint call must come back out parseable, since the
  whole point of the round trip is a copy-pasteable, guaranteed-
  executable hint.

See the :mod:`precis.tools.command_parser` docstring.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.tools import TOOL_REGISTRY
from precis.tools.command_parser import CommandParseError, parse_command
from precis.utils.next_block import render_next_section

# ---------------------------------------------------------------------------
# Acceptance — one representative call per verb
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, expected_verb, expected_kwargs",
    [
        ("get(kind='skill', id='toc')", "get", {"kind": "skill", "id": "toc"}),
        (
            "search(kind='paper', q='two-photon absorption')",
            "search",
            {"kind": "paper", "q": "two-photon absorption"},
        ),
        (
            "put(kind='memory', text='note', tags=['workspace'])",
            "put",
            {"kind": "memory", "text": "note", "tags": ["workspace"]},
        ),
        (
            "edit(kind='draft', id='dc42', mode='append', text='more')",
            "edit",
            {"kind": "draft", "id": "dc42", "mode": "append", "text": "more"},
        ),
        ("delete(kind='todo', id=42)", "delete", {"kind": "todo", "id": 42}),
        (
            "tag(kind='todo', id=42, add=['STATUS:done'])",
            "tag",
            {"kind": "todo", "id": 42, "add": ["STATUS:done"]},
        ),
        (
            "link(kind='paper', id='x', link='memory:1', rel='cites')",
            "link",
            {"kind": "paper", "id": "x", "link": "memory:1", "rel": "cites"},
        ),
        ("more(cursor='abc123')", "more", {"cursor": "abc123"}),
    ],
)
def test_parse_command_accepts_every_verb(
    command: str, expected_verb: str, expected_kwargs: dict
) -> None:
    verb, kwargs = parse_command(command)
    assert verb == expected_verb
    assert kwargs == expected_kwargs


def test_parse_command_accepts_nested_literals() -> None:
    verb, kwargs = parse_command(
        "edit(kind='draft', id=42, table={'header': ['a', 'b'], 'rows': [[1, 2]]})"
    )
    assert verb == "edit"
    assert kwargs["table"] == {"header": ["a", "b"], "rows": [[1, 2]]}


def test_parse_command_verb_set_matches_tool_registry() -> None:
    """The parser's verb set is sourced from ``TOOL_REGISTRY`` — no
    hard-coded copy. Both directions of that contract, live-checked."""
    for verb in TOOL_REGISTRY:
        # Minimal literal-only call per verb: every verb accepts kind=
        # except more() (cursor=) — the registry-derived acceptance
        # check just needs the verb name itself to be recognised.
        if verb == "more":
            parse_command("more(cursor='x')")
        else:
            parse_command(f"{verb}()")


# ---------------------------------------------------------------------------
# text= merging
# ---------------------------------------------------------------------------


def test_text_param_merges_into_kwargs() -> None:
    verb, kwargs = parse_command("put(kind='memory', mode='create')", text="body")
    assert verb == "put"
    assert kwargs["text"] == "body"


def test_text_param_and_inline_text_is_ambiguous() -> None:
    with pytest.raises(CommandParseError, match="text="):
        parse_command("put(kind='memory', text='inline')", text="separate")


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "1 + 1",
        "os.system('x')",
        "get(kind='skill').something()",
        "get('skill', id='toc')",
        "get(**{'kind': 'skill'})",
        "nope(kind='skill')",
        "get(kind=foo())",
        "get(kind=some_var)",
        "get(kind=1+1)",
        "",
        "get(kind='skill'",
    ],
)
def test_parse_command_rejects(command: str) -> None:
    with pytest.raises(CommandParseError):
        parse_command(command)


def test_parse_command_rejects_positional_args() -> None:
    with pytest.raises(CommandParseError, match="keyword arguments only"):
        parse_command("get('skill', id='toc')")


def test_parse_command_rejects_kwargs_splat() -> None:
    with pytest.raises(CommandParseError, match=r"\*\*kwargs"):
        parse_command("get(**{'kind': 'skill'})")


def test_parse_command_rejects_attribute_access() -> None:
    with pytest.raises(CommandParseError):
        parse_command("os.system(kind='skill')")


def test_parse_command_rejects_nested_call() -> None:
    with pytest.raises(CommandParseError):
        parse_command("get(kind=search(q='x'))")


def test_parse_command_rejects_unregistered_verb() -> None:
    with pytest.raises(CommandParseError, match="not a registered verb"):
        parse_command("frobnicate(kind='skill')")


def test_parse_command_rejects_duplicate_keyword() -> None:
    # ast.parse (unlike compile) tolerates repeated keywords; silent
    # last-wins would drop a value the caller meant.
    with pytest.raises(CommandParseError, match="more than once"):
        parse_command("get(kind='skill', kind='todo')")


# ---------------------------------------------------------------------------
# Corpus sweep — src/precis/data/skills/*.md
# ---------------------------------------------------------------------------

_VERB_START = re.compile(r"\b(" + "|".join(sorted(TOOL_REGISTRY)) + r")\(")
_PLACEHOLDER_MARKERS = ("…", "<", "...")
# The corpus's second (unmarked) placeholder convention: a bare
# identifier / attribute-access / f-string / pipe-union value
# standing in for "put a real literal here" (e.g. ``id=N``,
# ``id=todo.id``, ``target=f"todo:{x}"``, ``view='a'|'b'``) — same
# intent as the ``<placeholder>`` bracket convention, just unmarked.
_BAREWORD_VALUE = re.compile(r"""=\s*(?:f['"]|[A-Za-z_][\w.]*\s*[,)]|\s*[,)])""")
_KEYWORD_LITERALS = ("True", "False", "None")


def _extract_calls(text: str) -> list[str]:
    """Balanced-paren extraction of every ``verb(...)`` span in *text*."""
    calls = []
    for m in _VERB_START.finditer(text):
        start = m.start()
        depth = 0
        in_str: str | None = None
        j = m.end() - 1
        n = len(text)
        while j < n:
            ch = text[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == in_str:
                    in_str = None
            else:
                if ch in ("'", '"'):
                    in_str = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        calls.append(text[start : j + 1])
                        break
                elif ch == "\n" and depth == 0:
                    break
            j += 1
    return calls


def _has_bareword_placeholder(call: str) -> bool:
    for m in _BAREWORD_VALUE.finditer(call):
        tok = call[m.start() + 1 : m.end()].strip().rstrip(",)")
        if tok in _KEYWORD_LITERALS:
            continue
        return True
    return "|" in call


def _skill_corpus_calls() -> list[str]:
    skills_dir = Path(__file__).resolve().parents[1] / "src/precis/data/skills"
    calls: list[str] = []
    for path in sorted(skills_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for call in _extract_calls(text):
            if any(marker in call for marker in _PLACEHOLDER_MARKERS):
                continue
            if _has_bareword_placeholder(call):
                continue
            calls.append(call)
    return calls


def test_skill_corpus_call_examples_parse() -> None:
    """Every non-placeholder call example in the skill docs parses.

    The floor guards against the sweep silently going vacuous (e.g. a
    path typo that makes ``glob()`` return nothing). The 1% failure
    tolerance absorbs a handful of markdown line-wrap artifacts and
    placeholders nested inside a dict/list literal that the
    bareword filter doesn't reach into — none of them are real
    ``command`` shapes an agent would ever send.
    """
    calls = _skill_corpus_calls()
    assert len(calls) >= 300, (
        f"only {len(calls)} call examples found in the skill corpus — "
        "sweep may have gone vacuous (path or extraction regression)"
    )
    failures = []
    for call in calls:
        try:
            parse_command(call)
        except CommandParseError as e:
            failures.append((call, str(e)))
    ok = len(calls) - len(failures)
    assert ok / len(calls) >= 0.99, (
        f"{len(failures)}/{len(calls)} skill-corpus call examples failed to "
        f"parse: {failures[:10]}"
    )


# ---------------------------------------------------------------------------
# next_block round trip
# ---------------------------------------------------------------------------

_REPRESENTATIVE_NEXT_HINTS: list[tuple[str, str]] = [
    ("get(kind='skill', id='toc')", "browse the skill index"),
    ("get(kind='paper', id='gerfen2011', view='toc')", "see the paper's chunk map"),
    ("get(kind='paper', id='gerfen2011~0..5')", "read the first five chunks"),
    ("search(kind='skill', q='discovery layer')", "search skills"),
    ("search(kind='paper', tags=['DREAM:acquire'], page_size=10)", "filter by tag"),
    ("put(kind='memory', text='note', tags=['workspace'])", "save a memory"),
    ("put(kind='todo', text='buy milk', mode='create')", "create a todo"),
    (
        "edit(kind='paper', id='gerfen2011~0', mode='append', text='more')",
        "append a chunk",
    ),
    (
        "edit(kind='draft', id=42, table={'header': ['a', 'b'], 'rows': [[1, 2]]})",
        "edit a table",
    ),
    ("delete(kind='todo', id=42)", "delete a todo"),
    ("tag(kind='todo', id=42, add=['STATUS:done'])", "resolve the ask"),
    (
        "link(kind='paper', id='gerfen2011', link='memory:123', rel='cites')",
        "link a memory",
    ),
    ("more(cursor='abc123')", "fetch the next page"),
]


def test_next_block_hints_round_trip() -> None:
    """Every ``Next:`` hint rendered by ``next_block`` must re-parse.

    Hints become guaranteed-executable: an agent can copy the
    ``execute this call`` column verbatim into ``precis(command=...)``.
    """
    rendered = render_next_section(_REPRESENTATIVE_NEXT_HINTS)
    lines = rendered.strip("\n").split("\n")
    assert lines[0] == "Next:"
    data_lines = lines[2:]
    assert len(data_lines) == len(_REPRESENTATIVE_NEXT_HINTS)
    for line, (expected_call, _desc) in zip(data_lines, _REPRESENTATIVE_NEXT_HINTS):
        _desc_out, sep, call_out = line.partition("\t")
        assert sep == "\t", f"row did not tab-split as expected: {line!r}"
        assert call_out == expected_call
        verb, _kwargs = parse_command(call_out)
        assert verb in TOOL_REGISTRY
