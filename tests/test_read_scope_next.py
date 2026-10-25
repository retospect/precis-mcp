"""gr512233 slice 2: ``Next:``, the hint bus and ``next=`` in read scope.

A read-scope connection can't run write recipes, so surfaces that print
recipe-shaped calls filter them through
:func:`precis.utils.skill_scope.is_write_call`:

* ``render_next_section`` drops write rows — and renders no ``Next:``
  header at all when none survive;
* ``HintBus.collect`` drops hints whose text advertises a write call;
* ``ErrorMixin.render_error`` drops write ``next=`` entries — when all
  drop, one pointer line replaces them.

The text-only gripe create (``put(kind='gripe', text=...)``) is the
read-only carve-out and survives everywhere. Write scope is untouched.
"""

from __future__ import annotations

import pytest

import precis.runtime.scope as scope_mod
from precis.errors import BadInput
from precis.hints import Hint, HintBus
from precis.runtime.error import ErrorMixin
from precis.utils.next_block import render_next_section


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


ROWS = [
    ("get(kind='memory', id='me1')", "read the memory"),
    ("put(kind='memory', text='x')", "jot a note"),
    ("put(kind='gripe', text='misled')", "report a problem"),
    ("edit(kind='memory', id='me1', find='a', replace='b')", "fix it"),
]


def test_next_section_drops_write_rows_in_read_scope(read_scope: None) -> None:
    body = render_next_section(ROWS)
    assert "Next:" in body
    assert "get(kind='memory'" in body
    assert "put(kind='gripe', text='misled')" in body  # the carve-out
    assert "put(kind='memory'" not in body
    assert "edit(kind='memory'" not in body


def test_next_section_all_writes_renders_no_header(read_scope: None) -> None:
    body = render_next_section(
        [("put(kind='memory', text='x')", "jot"), ("tag(kind='x', id=1)", "tag")]
    )
    assert body == ""
    assert "Next:" not in body


def test_next_section_write_scope_keeps_every_row() -> None:
    body = render_next_section(ROWS)
    for call, _ in ROWS:
        assert call in body


def test_hint_bus_drops_write_recipe_hints(read_scope: None) -> None:
    bus = HintBus()
    with bus.request():
        bus.emit(Hint(text="retry with put(kind='memory', text='x')", topic="a"))
        bus.emit(Hint(text="report it: put(kind='gripe', text='misled')", topic="b"))
        bus.emit(Hint(text="see get(kind='memory', id='me1')", topic="c"))
        hints = bus.collect()
    texts = [h.text for h in hints]
    assert not any("put(kind='memory'" in t for t in texts)
    assert any("put(kind='gripe'" in t for t in texts)  # the carve-out
    assert any("get(kind='memory'" in t for t in texts)


def test_hint_bus_write_scope_keeps_everything() -> None:
    bus = HintBus()
    with bus.request():
        bus.emit(Hint(text="retry with put(kind='memory', text='x')", topic="a"))
        hints = bus.collect()
    assert hints and "put(kind='memory'" in hints[0].text


def _render(err: BadInput) -> str:
    return ErrorMixin().render_error(err)


def test_render_error_all_write_next_prints_the_pointer(read_scope: None) -> None:
    err = BadInput(
        "nope",
        next=["put(kind='memory', text='x')", "edit(kind='memory', id='me1')"],
    )
    body = _render(err)
    assert "put(kind='memory'" not in body
    assert "edit(kind='memory'" not in body
    assert "next: (write recipe omitted: this connection is read-only" in body
    assert "put(kind='gripe', text='...')" in body  # the door stays open
    assert body.count("next:") == 1


def test_render_error_mixed_next_keeps_read_hints(read_scope: None) -> None:
    err = BadInput(
        "nope",
        next=[
            "get(kind='skill', id='precis-overview')",
            "delete(kind='memory', id='me1')",
        ],
    )
    body = _render(err)
    assert "next: get(kind='skill'" in body
    assert "delete(kind='memory'" not in body
    assert "write recipe omitted" not in body


def test_render_error_string_next_write_also_pointers(read_scope: None) -> None:
    err = BadInput("nope", next="put(kind='memory', text='x')")
    body = _render(err)
    assert "next: (write recipe omitted" in body


def test_render_error_gripe_create_next_survives(read_scope: None) -> None:
    err = BadInput("nope", next="put(kind='gripe', text='what went wrong')")
    body = _render(err)
    assert "next: put(kind='gripe', text='what went wrong')" in body


def test_render_error_write_scope_is_unchanged() -> None:
    err = BadInput(
        "nope", next=["put(kind='memory', text='x')", "get(kind='memory', id='me1')"]
    )
    body = _render(err)
    assert "next: put(kind='memory', text='x')" in body
    assert "next: get(kind='memory', id='me1')" in body


# ── hintcheck's read-scope guard ─────────────────────────────────────


def test_hintcheck_flags_a_write_hint_in_read_scope(read_scope: None) -> None:
    """A hand-built trailer that bypasses the renderer and still
    advertises a write call fails ``assert_hints_round_trip`` — the
    slice-2 ratchet lists today's sites; this guards tomorrow's."""
    from tests.hintcheck import assert_hints_round_trip

    body = "rows\n\nNext:\nput(kind='memory', text='x')\tjot a note\n"
    with pytest.raises(AssertionError, match="advertises write calls"):
        assert_hints_round_trip(body, lambda v, k: "ok")


def test_hintcheck_passes_a_clean_read_scope_trailer(read_scope: None) -> None:
    from tests.hintcheck import assert_hints_round_trip

    body = (
        "rows\n\nNext:\n"
        "get(kind='memory', id='me1')\tread it\n"
        "put(kind='gripe', text='<what>')\treport a problem\n"
    )
    hints = assert_hints_round_trip(body, lambda v, k: "ok")
    assert len(hints) == 2
