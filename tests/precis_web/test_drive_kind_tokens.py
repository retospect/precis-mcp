"""gr478511: leading ``/kind`` tokens in the Drive query become kind filters."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.drive import _split_kind_tokens

KINDS = ["draft", "paper", "se"]
TEMPLATE = (
    Path(__file__).resolve().parents[2] / "src/precis_web/templates/drive/index.html.j2"
).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("q", "want"),
    [
        ("/draft nano", (["draft"], "nano")),
        ("/drafts nano", (["draft"], "nano")),
        ("/Drafts", (["draft"], "")),
        ("/draft /paper  two words", (["draft", "paper"], "two words")),
        ("/draft /draft x", (["draft"], "x")),
        ("nano /draft", ([], "nano /draft")),
        ("/unknown nano", ([], "/unknown nano")),
        ("/draft /unknown nano", (["draft"], "/unknown nano")),
        ("/ nano", ([], "/ nano")),
        ("", ([], "")),
    ],
)
def test_split_kind_tokens(q, want):
    assert _split_kind_tokens(q, KINDS) == want


def test_route_draft_token_scopes_kind_and_keeps_text(client, runtime):
    response = client.get("/drive?q=/drafts+nano&submitted=1")
    assert response.status_code == 200
    assert "No matches for /drafts nano" not in response.text


def test_query_input_restores_caret_and_hint_present():
    assert "setSelectionRange($el.value.length, $el.value.length)" in TEMPLATE
    assert "Syntax: <code>/draft nano</code>" in TEMPLATE
