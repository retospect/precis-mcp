"""The gripe detail heading shows its title without repeating the body lead."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")


@pytest.mark.parametrize(
    ("stored_title", "body"),
    [
        ("Missing title", "Details remain visible."),
        ("Missing title", "Missing title\n\nDetails remain visible."),
        (
            "Missing title\n\nDetails remain visible.",
            "Missing title\n\nDetails remain visible.",
        ),
        ("Missing title", "\n  Missing title\r\n\r\nDetails remain visible."),
    ],
)
def test_gripe_detail_title_and_body(runtime, client, stored_title, body) -> None:
    """gr559112: separate and legacy titles both become a visible heading."""
    runtime.store.gripes[0].title = stored_title
    runtime.store._conv_blocks[96][0].text = body

    response = client.get("/gripes/96")

    assert response.status_code == 200
    assert re.search(r"<h1\b[^>]*>Missing title</h1>", response.text)
    assert response.text.count("Missing title") == 1
    assert response.text.count("Details remain visible.") == 1
    assert "only triggers when the slug has a hyphen" in response.text
    assert runtime.store._conv_blocks[96][0].text == body


def test_gripe_detail_keeps_distinct_body_first_line(runtime, client) -> None:
    runtime.store.gripes[0].title = "Short title"
    body = "Short title with extra context.\nMore detail."
    runtime.store._conv_blocks[96][0].text = body

    response = client.get("/gripes/96")

    assert re.search(r"<h1\b[^>]*>Short title</h1>", response.text)
    assert "Short title with extra context." in response.text
    assert "More detail." in response.text


def test_gripe_detail_without_body_chunk_uses_title(runtime, client) -> None:
    runtime.store.gripes[0].title = "Legacy title\n\nLegacy details."
    runtime.store._conv_blocks[96] = []

    response = client.get("/gripes/96")

    assert re.search(r"<h1\b[^>]*>Legacy title</h1>", response.text)
    assert response.text.count("Legacy title") == 1
    assert "Legacy details." in response.text


def test_gripe_detail_title_escapes_html(runtime, client) -> None:
    runtime.store.gripes[0].title = "Missing <script>alert('title')</script>"
    runtime.store._conv_blocks[96][0].text = runtime.store.gripes[0].title

    response = client.get("/gripes/96")

    assert "<script>alert('title')</script>" not in response.text
    assert (
        "Missing &lt;script&gt;alert(&#x27;title&#x27;)&lt;/script&gt;" in response.text
    )
