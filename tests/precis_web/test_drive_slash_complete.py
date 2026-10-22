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
    assert names == _roster() | {"paper+chunks"}
    for i in items:
        assert i["insert"] == f"/{i['name']}"
        assert i["desc"]


def test_complete_filters_by_prefix(client) -> None:
    items = client.get("/drive/complete", params={"text": "/pa"}).json()
    names = {i["name"] for i in items}
    assert names == {k for k in _roster() if k.startswith("pa")} | {"paper+chunks"}
    assert {"paper", "paper+chunks", "patent", "pathway"} <= names
    chunks = next(i for i in items if i["name"] == "paper+chunks")
    assert chunks["insert"] == "/paper+chunks"
    assert chunks["desc"] == "papers with chunks"


def test_complete_paper_chunks_token_prefixes(client) -> None:
    for text in ("/paper+", "/paper+ch", "/papers"):
        names = {
            i["name"]
            for i in client.get("/drive/complete", params={"text": text}).json()
        }
        assert "paper+chunks" in names, text
    names = {
        i["name"] for i in client.get("/drive/complete", params={"text": "/pat"}).json()
    }
    assert "paper+chunks" not in names


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
        "//foo",  # escaped literal (slice 2) — no completions
        "nano /draft ",  # trailing space after a command
        "/draft since:soon",  # qualifier-shaped but not a qualifier
    ],
)
def test_complete_non_command_gets_nothing(client, text: str) -> None:
    assert client.get("/drive/complete", params={"text": text}).json() == []


# ── slice 2: bare-word qualifier completions ────────────────────────


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("i", "is:stub"),
        ("is", "is:stub"),
        ("is:", "is:stub"),
        ("is:s", "is:stub"),
        ("h", "has:body"),
        ("has", "has:body"),
        ("has:", "has:body"),
        ("tag", "tag:"),
        ("since", "since:"),
        ("/paper i", "is:stub"),  # after the kind tokens
        ("/draft nano ta", "tag:"),  # after free text, too
    ],
)
def test_complete_offers_qualifiers(client, text: str, want: str) -> None:
    """A bare trailing word that prefixes a qualifier completes to the
    qualifier command — ``is:``/``has:`` resolve their fixed value;
    ``tag:``/``since:`` insert the bare prefix (value typed next)."""
    names = {
        i["name"] for i in client.get("/drive/complete", params={"text": text}).json()
    }
    assert want in names, (text, names)


def test_complete_qualifier_unrecognised_value_gets_nothing(client) -> None:
    assert client.get("/drive/complete", params={"text": "is:x"}).json() == []
    assert client.get("/drive/complete", params={"text": "has:chunks"}).json() == []
    # ``since:`` values are free-form (N<d|h|w>) — nothing to offer.
    assert client.get("/drive/complete", params={"text": "since:7"}).json() == []


def test_complete_tag_values_come_from_the_roster(client) -> None:
    """``tag:<partial>`` completes from the tag roster (``suggest_tags`` —
    the FakeStore pool holds topic:co2-capture and topic:graphene)."""
    items = client.get("/drive/complete", params={"text": "tag:graph"}).json()
    assert [i["insert"] for i in items] == ["tag:topic:graphene"]
    assert items[0]["name"] == "topic:graphene"


# ── template: the debounced submit must not fire mid-command ────────


def test_query_input_routes_through_slash_guard():
    """The query box no longer submits unconditionally on input —
    onQueryInput holds the debounce while a "/token" is being typed."""
    assert '@input="onQueryInput()"' in TEMPLATE
    assert '@input="submitForm(1000)"' not in TEMPLATE
    assert "clearTimeout(this._t)" in TEMPLATE


def test_popdown_keyboard_and_mouse_contract():
    """Arrows move, Tab or Enter picks the highlighted row (gr500642 —
    Tab defaults to the first), Esc closes, mousedown picks before
    blur."""
    for marker in (
        "ArrowDown",
        "ArrowUp",
        "'Enter'",
        "'Tab'",
        "'Escape'",
        "mousedown.prevent",
        "/drive/complete",
    ):
        assert marker in TEMPLATE, marker


def test_popdown_tab_only_hijacks_with_a_suggestion():
    """Tab must not break normal focus navigation: the drive handler
    early-returns unless the popdown is open with rows, and Shift+Tab
    keeps its backwards-traversal meaning (gr500642)."""
    assert "if (!this.slashOpen || !this.slashItems.length) return;" in TEMPLATE
    assert "e.key === 'Tab' && !e.shiftKey" in TEMPLATE


# ── gr500642: same Tab-complete in the header's "search everything" box ──

BASE_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "src/precis_web/templates/base.html.j2"
).read_text(encoding="utf-8")


def test_nav_search_box_tab_completes_first_suggestion():
    """The header box carries the data-slash-complete hook and its own
    Tab branch over the same /drive/complete roster — with the roster's
    prefix/plural filter and the trailing-space insert the drive
    pickSlash writes."""
    assert "data-slash-complete" in BASE_TEMPLATE
    assert "'/drive/complete?text=%2F'" in BASE_TEMPLATE
    assert "e.key !== 'Tab' || e.shiftKey" in BASE_TEMPLATE
    assert "roster.find" in BASE_TEMPLATE


def test_nav_search_box_tab_falls_through_without_a_suggestion():
    """No "/token", a still-loading/empty roster or no prefix match all
    return before preventDefault, so plain Tab still moves focus."""
    assert "if (!m || roster === null) return;" in BASE_TEMPLATE
    assert "if (!first) return;" in BASE_TEMPLATE
    # preventDefault must come AFTER those guards, via .call — the
    # base-template empty-parens ban covers every call site.
    guard = BASE_TEMPLATE.index("if (!first) return;")
    hijack = BASE_TEMPLATE.index("e.preventDefault.call(e);")
    assert guard < hijack
