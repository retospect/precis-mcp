"""gr486407: the drive query box's slash-command popdown.

``GET /drive/complete`` serves one row per known kind — the same roster
``_split_kind_tokens`` resolves — so a ``/token`` typed in the box gets
completions while a pasted ``/c/<handle>`` path or plain text gets none.
The template half: the query input's debounced submit must stay off
while the input is an incomplete slash command (the "reload" the gripe
reported was the debounced GET firing mid-command).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.drive import _kind_buckets

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "src/precis_web/templates/drive/index.html.j2"
).read_text(encoding="utf-8")


def _roster() -> set[str]:
    """The kind set the popdown serves with no hub/cookie — the fallback
    roster ``index`` also resolves (FakeRuntime.hub is None)."""
    return {k for kinds in _kind_buckets(None).values() for k in kinds}


def test_complete_lists_every_registered_kind(client) -> None:
    """Registry parity: ``/`` alone offers every kind command the parser
    accepts, each with its one-line description."""
    items = client.get("/drive/complete", params={"text": "/"}).json()
    names = {i["name"] for i in items}
    assert names == _roster()
    for i in items:
        assert i["insert"] == f"/{i['name']}"
        assert i["desc"]


def test_complete_filters_by_prefix(client) -> None:
    items = client.get("/drive/complete", params={"text": "/pa"}).json()
    names = {i["name"] for i in items}
    assert names == {k for k in _roster() if k.startswith("pa")}
    assert {"paper", "patent", "pathway"} <= names


def test_complete_plural_prefix(client) -> None:
    """``/papers`` still offers ``/paper`` — the parser takes plurals."""
    items = client.get("/drive/complete", params={"text": "/papers"}).json()
    assert "paper" in {i["name"] for i in items}


def test_complete_second_kind_token(client) -> None:
    """A further ``/token`` after a complete command completes too —
    ``/draft /pa`` narrows the second kind filter."""
    items = client.get("/drive/complete", params={"text": "/draft /pa"}).json()
    assert "paper" in {i["name"] for i in items}


def test_complete_cursor_position(client) -> None:
    """``pos`` slices the input — a token mid-value completes."""
    items = client.get("/drive/complete", params={"text": "/pa nano", "pos": 3}).json()
    assert "paper" in {i["name"] for i in items}


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty input
        "nano",  # plain text
        "/draft nano",  # complete command + free text
        "/draft ",  # complete command, trailing space
        "/c/qu123",  # pasted site path (gr484947 — not a command)
        "https://x.example/drive",  # pasted URL
        "/draft /c/qu1",  # path-shaped token after a command
    ],
)
def test_complete_non_command_gets_nothing(client, text: str) -> None:
    assert client.get("/drive/complete", params={"text": text}).json() == []


# ── template: the debounced submit must not fire mid-command ────────


def test_query_input_routes_through_slash_guard():
    """The query box no longer submits unconditionally on input —
    onQueryInput holds the debounce while a "/token" is being typed."""
    assert '@input="onQueryInput()"' in TEMPLATE
    assert '@input="submitForm(1000)"' not in TEMPLATE
    assert "clearTimeout(this._t)" in TEMPLATE


def test_popdown_keyboard_and_mouse_contract():
    """Arrows move, Enter picks, Esc closes, mousedown picks before blur."""
    for marker in (
        "ArrowDown",
        "ArrowUp",
        "'Enter'",
        "'Escape'",
        "mousedown.prevent",
        "/drive/complete",
    ):
        assert marker in TEMPLATE, marker
