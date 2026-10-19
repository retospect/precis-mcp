"""gr478511: leading ``/kind`` tokens in the Drive query become kind filters."""

from __future__ import annotations

import re
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
        ("/draft nano", (["draft"], "nano", False)),
        ("/drafts nano", (["draft"], "nano", False)),
        ("/Drafts", (["draft"], "", False)),
        ("/draft /paper  two words", (["draft", "paper"], "two words", False)),
        ("/draft /draft x", (["draft"], "x", False)),
        ("nano /draft", ([], "nano /draft", False)),
        ("/unknown nano", ([], "/unknown nano", False)),
        ("/draft /unknown nano", (["draft"], "/unknown nano", False)),
        ("/ nano", ([], "/ nano", False)),
        ("", ([], "", False)),
        ("/paper+chunks", (["paper"], "", True)),
        ("/papers+chunks nano", (["paper"], "nano", True)),
        ("/Paper+Chunks /draft x", (["paper", "draft"], "x", True)),
        ("/draft+chunks nano", ([], "/draft+chunks nano", False)),
        ("/paper+bogus nano", ([], "/paper+bogus nano", False)),
        ("/paper nano", (["paper"], "nano", False)),
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


def test_route_paper_chunks_token_matches_url_params(client, runtime):
    """gr498598: ``/paper+chunks`` gives the same result set, select and
    chip as ``?k=paper&paper_chunks=with``."""
    via_token = client.get("/drive?q=/paper%2Bchunks&submitted=1")
    assert via_token.status_code == 200
    assert runtime.store.recent_has_chunks is True
    via_params = client.get("/drive?k=paper&paper_chunks=with&submitted=1")
    assert via_params.status_code == 200
    assert via_token.text.count("Body: with") == via_params.text.count("Body: with") > 0
    assert re.search(r'<option value="with"[^>]*selected', via_token.text)
    assert "paper_chunks=with" in via_token.text


def test_route_plain_paper_token_leaves_chunks_unset(client, runtime):
    client.get("/drive?q=/paper&submitted=1")
    assert runtime.store.recent_has_chunks is None
