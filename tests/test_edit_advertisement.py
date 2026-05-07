"""Pins the ``edit``-verb advertisement across three surfaces.

MCP critic (2026-05-03) traced a small-model retry loop back to the
fact that every surface (schema, tool docstring, per-kind skills)
played down the per-mode required-args coupling. These tests lock
the three surfaces together so a future refactor can't regress one
without failing the others.

Tests cover:

- **Tool docstrings**: the ``edit`` tool docstring has no hedge
  language and explicitly documents ``text=''`` as the span-delete
  idiom; the ``delete`` tool docstring lists both the whole-file
  clear recipe and the find-replace span-delete recipe.
- **Skills**: every edit-capable skill contains a ``text=''`` recipe
  so a small model reading the kind's help doc before calling the
  tool sees the delete idiom at least once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis import server

_SKILLS_DIR = Path(__file__).parent.parent / "src" / "precis" / "data" / "skills"

#: Skills documenting edit-capable kinds or the protocol itself.
#: Each of these must show ``text=''`` at least once so a small
#: model following the learn-path has a canonical example before
#: its first call.
_EDIT_CAPABLE_SKILLS = (
    "precis-edit-protocol.md",
    "precis-plaintext-help.md",
    "precis-markdown-help.md",
    "precis-tex-help.md",
    "precis-files-help.md",
    "precis-python-help.md",
)


# ---------------------------------------------------------------------------
# Tool docstrings
# ---------------------------------------------------------------------------


def test_edit_docstring_mentions_empty_text_as_delete_idiom() -> None:
    """The ``edit`` tool docstring must advertise ``text=''`` as the
    span-delete idiom in its text param block — small models read the
    per-field prose before the allOf schema."""
    doc = server.edit.__doc__ or ""
    assert "text=''" in doc, (
        "edit() docstring must contain the literal `text=''` so the "
        "delete idiom is discoverable from the tool signature alone"
    )


def test_edit_docstring_marks_text_as_required() -> None:
    """The text param entry must name the per-mode required coupling."""
    doc = server.edit.__doc__ or ""
    # Accept any of the explicit phrasings the author might pick.
    assert "**Required**" in doc, (
        "edit() docstring must call out required params explicitly; "
        "small models don't infer required-ness from prose hedges"
    )


def test_edit_docstring_has_no_hedges() -> None:
    """Hedge phrases signal 'optional' to 7B models. Strike on sight."""
    doc = (server.edit.__doc__ or "").lower()
    bad_phrases = (
        "(mode-dependent)",
        "can sometimes",
        "may return",
        "it is important",
        "please note",
    )
    offenders = [p for p in bad_phrases if p in doc]
    assert not offenders, (
        f"edit() docstring contains hedge phrase(s): {offenders!r}. "
        "These signal 'optional' to small models."
    )


def test_delete_docstring_lists_both_delete_idioms() -> None:
    """The ``delete`` tool docstring must point span-delete callers at
    ``edit(mode='find-replace', text='')`` — not only at the whole-file
    ``edit(mode='replace', text='')`` recipe."""
    doc = server.delete.__doc__ or ""
    assert "mode='replace', text=''" in doc, (
        "delete() docstring must mention the whole-file clear recipe "
        "(`edit(mode='replace', text='')`)"
    )
    assert "mode='find-replace'" in doc and "text=''" in doc, (
        "delete() docstring must mention the span-delete recipe "
        "(`edit(mode='find-replace', find='…', text='')`) — otherwise "
        "callers reach for the wrong verb when they want to drop one "
        "line from a file"
    )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _EDIT_CAPABLE_SKILLS)
def test_edit_capable_skill_documents_delete_idiom(name: str) -> None:
    """Every skill that an agent reads before calling ``edit`` must
    show the ``text=''`` delete idiom at least once. Without this,
    small models reading the skill only see find+text pairs and
    generalise to "always supply text" on replace but "never supply
    text" on delete, then get stuck on BadInput."""
    path = _SKILLS_DIR / name
    assert path.is_file(), f"skill {name!r} missing at {path}"
    body = path.read_text(encoding="utf-8")
    assert "text=''" in body, (
        f"skill {name!r} contains no `text=''` example — agents reading "
        "it before their first edit won't know how to delete a matched "
        "span and will loop on BadInput when they try"
    )
