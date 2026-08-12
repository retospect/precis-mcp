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

import inspect
from pathlib import Path

import pytest

from precis.dispatch import Hub

# The seven verbs moved out of ``precis.server`` into the shared
# tool registry (``precis.tools.core``) so the MCP server and the
# CLI consume the same callable. The advertisement tests pin the
# function-level docstring contract; importing the function from
# its definition site is the most direct way to access the same
# object FastMCP serialises into ``tools/list``.
from precis.tools import core as tools_core
from precis.tools.core import delete as delete_tool
from precis.tools.core import edit as edit_tool

_SKILLS_DIR = Path(__file__).parent.parent / "src" / "precis" / "data" / "skills"

#: Skills documenting edit-capable kinds or the protocol itself.
#: Each of these must show ``text=''`` at least once so a small
#: model following the learn-path has a canonical example before
#: its first call.
_EDIT_CAPABLE_SKILLS = (
    "precis-edit-help.md",
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
    doc = edit_tool.__doc__ or ""
    assert "text=''" in doc, (
        "edit() docstring must contain the literal `text=''` so the "
        "delete idiom is discoverable from the tool signature alone"
    )


def test_edit_docstring_marks_text_as_required() -> None:
    """The text param entry must name the per-mode required coupling."""
    doc = edit_tool.__doc__ or ""
    # Accept any of the explicit phrasings the author might pick.
    assert "**Required**" in doc, (
        "edit() docstring must call out required params explicitly; "
        "small models don't infer required-ness from prose hedges"
    )


def test_edit_docstring_has_no_hedges() -> None:
    """Hedge phrases signal 'optional' to 7B models. Strike on sight."""
    doc = (edit_tool.__doc__ or "").lower()
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
    doc = delete_tool.__doc__ or ""
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


# ---------------------------------------------------------------------------
# Wire-schema parameter coverage (gr192827 item 5)
# ---------------------------------------------------------------------------

#: draft-documented ``edit(...)`` params (precis-draft-help,
#: precis-proposal-help, precis-audio-help) that were entirely absent
#: from the wire ``edit`` tool's signature — no MCP client could ever
#: send them, and the server answered "requires text=/move=/style=/..."
#: as if the caller had passed nothing at all.
_ONCE_MISSING_DRAFT_EDIT_PARAMS = (
    "sub",
    "authors",
    "authoring",
    "word_target",
    "origin",
    "permission",
    "voice",
    "lang",
)


@pytest.mark.parametrize("name", _ONCE_MISSING_DRAFT_EDIT_PARAMS)
def test_edit_tool_signature_exposes_draft_params(name: str) -> None:
    """precis-draft-help documents ``edit(sub=…)``, ``edit(authors=…)``,
    etc — the wire-level ``edit`` tool must actually declare these
    params so a strict-schema client can send them (gr192827 item 5)."""
    params = inspect.signature(edit_tool).parameters
    assert name in params, (
        f"edit tool is missing {name!r} — draft-help documents "
        f"edit({name}=...) but no client could ever send it"
    )


def test_edit_tool_sub_reaches_draft_handler(hub: Hub) -> None:
    """Before the fix, ``tools_core.edit(kind='draft', sub=...)`` raised
    ``TypeError: unexpected keyword argument 'sub'`` — the param wasn't
    declared on the wire function's signature at all, so no MCP client
    could ever reach the draft handler's regex-substitute op. Drive it
    end-to-end through the exact callable FastMCP invokes."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime

    store = hub.live_store
    runtime = PrecisRuntime(
        config=PrecisConfig(), hub=boot(store=store, embedder=hub.embedder)
    )
    tools_core._runtime = runtime
    try:
        proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
        tools_core.put(kind="draft", id="subtest", title="T", project=proj)
        ref = store.get_ref(kind="draft", id="subtest")
        assert ref is not None
        title_handle = store.reading_order(ref.id)[0].handle
        tools_core.put(
            kind="draft",
            id="subtest",
            chunk_kind="paragraph",
            text="alpha—beta",
            at={"after": "¶" + title_handle},
        )
        out = tools_core.edit(
            kind="draft",
            id="subtest",
            sub={"find": "—", "replace": ", "},
            apply=True,
        )
        assert "[error:" not in str(out)
        texts = [c.text for c in store.reading_order(ref.id) if c.text]
        assert any("alpha, beta" in t for t in texts)
    finally:
        tools_core._runtime = None
