"""gr631696: the gripe standing order reaches the agent at the point of friction.

Reto's standing order (2026-11-11): whenever the precis MCP does
something broken or bad, or a skill does not match its tool, file a
gripe right then, without asking. The order used to live only in graph
memory — visible at recall time, not at failure time. These pin the
three surfaces it now rides:

* ``server._INSTRUCTIONS`` — the cold-start banner names the filing
  call next to ``Discover:``;
* ``ErrorMixin.render_error`` — every argument-shaped refusal
  (``BadInput``/``Unsupported``) ack carries the gripe ``next:`` line,
  whether it came from a handler, boundary validation, or the command
  profile's kwarg ``TypeError``;
* ``precis-gripe-help`` — the skill states the order in its preamble.
"""

from __future__ import annotations

import pytest

import precis.runtime.scope as scope_mod
from precis.errors import BadInput, Denied, Internal, NotFound, PrecisError, Unsupported
from precis.runtime.error import _ARG_SHAPE_GRIPE_HINT, ErrorMixin


def _render(err: PrecisError) -> str:
    return ErrorMixin().render_error(err)


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


# ── server._INSTRUCTIONS ─────────────────────────────────────────────


def test_instructions_name_the_gripe_filing_call() -> None:
    from precis import server

    assert "put(kind='gripe'" in server._INSTRUCTIONS
    assert server._INSTRUCTIONS.index("Discover:") < server._INSTRUCTIONS.index(
        "put(kind='gripe'"
    )


# ── render_error: the gripe next: line on argument-shaped refusals ────


def test_bad_input_ack_carries_the_gripe_hint() -> None:
    body = _render(BadInput("unexpected keyword argument 'limit'"))
    assert f"next: {_ARG_SHAPE_GRIPE_HINT}" in body


def test_unsupported_ack_carries_the_gripe_hint() -> None:
    body = _render(Unsupported("kind='se' has no view='schematic'"))
    assert f"next: {_ARG_SHAPE_GRIPE_HINT}" in body


def test_gripe_hint_appended_after_the_raise_site_next() -> None:
    """The handler's own recovery hint stays first — the standing order
    is the trailing meta-hint, not a replacement."""
    err = BadInput("bad payload", next="retry with kind='todo'")
    body = _render(err)
    assert "next: retry with kind='todo'" in body
    assert body.index("next: retry") < body.index(_ARG_SHAPE_GRIPE_HINT)


def test_gripe_hint_not_duplicated_when_next_already_files_one() -> None:
    """A raise site already pointing at a gripe filing wins over the
    generic line — one gripe hint per ack, not two."""
    err = BadInput(
        "bad template", next="this is a template defect; file a gripe naming it"
    )
    body = _render(err)
    assert body.count("next:") == 1
    assert "file a gripe naming it" in body


@pytest.mark.parametrize("err_cls", [NotFound, Denied, Internal])
def test_non_argument_errors_carry_no_gripe_hint(err_cls: type[PrecisError]) -> None:
    """A missing ref, a permissions refusal and an infra fault are not
    call-shape failures — the hint would be noise there."""
    body = _render(err_cls("some failure", next="some recovery"))
    assert _ARG_SHAPE_GRIPE_HINT not in body


def test_gripe_hint_survives_read_scope(read_scope: None) -> None:
    """The create-only gripe filing is the read-only carve-out — a
    scoped-down reader still gets told to file."""
    err = BadInput("nope", next="get(kind='skill', id='precis-get-help')")
    body = _render(err)
    assert f"next: {_ARG_SHAPE_GRIPE_HINT}" in body


def test_gripe_hint_dedups_against_the_read_scope_pointer(read_scope: None) -> None:
    """When every ``next=`` drops under read scope the GENERIC_POINTER
    already names the gripe filing — the standing-order line does not
    stack a second gripe pointer on top."""
    err = BadInput("nope", next=["put(kind='memory', text='x')"])
    body = _render(err)
    assert "write recipe omitted" in body
    assert body.count("next:") == 1


# ── precis-gripe-help preamble ────────────────────────────────────────


def test_gripe_help_skill_states_the_standing_order() -> None:
    from tests._skill_roots import skill_path

    text = skill_path("precis-gripe-help").read_text(encoding="utf-8")
    preamble = text.split("\n## ", 1)[0]
    assert "Standing order" in preamble
    assert "put(kind='gripe'" in preamble
