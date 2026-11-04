"""Memory body tables use the same escaped handle rendering as prose."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from precis_web.linkify import linkify_refs, linkify_toon

_HANDLES = {"me474326": ("memory", False, 474326)}
_HREF = 'href="/refs/memory/474326"'


def test_markdown_table_header_and_cells_link_like_prose() -> None:
    body = (
        "See me474326.\n\n"
        "| me474326 | note |\n"
        "| --- | --- |\n"
        "| me474326 | <script>bad</script> & more |\n\n"
        "- See me474326."
    )
    rendered = str(linkify_toon(body, handles=_HANDLES))

    assert rendered.count(_HREF) == 4
    assert re.search(r"<th\b[^>]*>.*?" + re.escape(_HREF), rendered)
    assert re.search(r"<td\b[^>]*>.*?" + re.escape(_HREF), rendered)
    assert "<ul><li>See " in rendered
    assert "<script>bad</script>" not in rendered
    assert "&lt;script&gt;bad&lt;/script&gt; &amp; more" in rendered


@pytest.mark.parametrize("token", ["me474326", "[me474326]", "memory:474326"])
def test_table_and_prose_code_spans_keep_handles_literal(token: str) -> None:
    body = f"`{token}`\n\n| code |\n| --- |\n| `{token}` |"
    rendered = str(linkify_toon(body, handles=_HANDLES))

    assert "<table" in rendered
    assert rendered.count("<code ") == 2
    assert rendered.count(token) == 2
    assert "href=" not in rendered


def test_unresolved_handles_stay_plain_in_tables() -> None:
    rendered = str(linkify_toon("| handle |\n| --- |\n| me999999 |", handles=_HANDLES))

    assert "<table" in rendered
    assert "me999999</td>" in rendered
    assert "href=" not in rendered


def test_many_code_spans_do_not_turn_placeholders_into_citations() -> None:
    body = " ".join("`me474326`" for _ in range(12))

    rendered = str(linkify_toon(body, handles=_HANDLES))

    assert rendered.count("<code ") == 12
    assert rendered.count("me474326") == 12
    assert "href=" not in rendered
    assert "\x00" not in rendered


def test_tsv_tables_keep_links_and_header() -> None:
    body = "{handle\tnote}\nme474326\tmemory:474326"
    rendered = str(linkify_toon(body, handles=_HANDLES))

    assert "<thead>" in rendered
    assert _HREF in rendered
    assert 'href="/r/memory/474326"' in rendered
    assert rendered.count("<td ") == 2


def test_tsv_rows_starting_with_pipe_remain_tsv() -> None:
    rendered = str(linkify_toon("| left\t1\n| right\t2"))

    assert "<table" in rendered
    assert rendered.count("<tr>") == 2
    assert rendered.count("<td ") == 4
    assert "| left</td>" in rendered
    assert "| right</td>" in rendered


@pytest.mark.parametrize("fence", ["```", "~~~~"])
@pytest.mark.parametrize("closed", [True, False])
def test_fenced_table_is_literal(fence: str, closed: bool) -> None:
    body = f"{fence}\n| handle |\n| --- |\n| me474326 |\n"
    if closed:
        body += fence
    rendered = str(linkify_toon(body, handles=_HANDLES))

    assert "<pre><code>" in rendered
    assert "| me474326 |" in rendered
    assert "<table" not in rendered
    assert "href=" not in rendered


def test_bare_handle_redirect_default_is_unchanged() -> None:
    rendered = str(linkify_refs("me474326", handles=_HANDLES))

    assert 'href="/r/memory/474326"' in rendered
    assert _HREF not in rendered


@pytest.mark.parametrize("wrapper", ["`{}`", "```\n{}\n```", "~~~~\n{}\n~~~~"])
def test_code_dois_stay_literal(wrapper: str) -> None:
    doi = "doi:10.1000/example"
    body = wrapper.format(doi) + "\n\n" + doi

    rendered = str(
        linkify_toon(body, doi_links={"10.1000/example": "/papers/1"}, handles={})
    )

    code = re.search(r"<code\b[^>]*>(.*?)</code>", rendered, re.DOTALL)
    assert code is not None
    assert code.group(1) == doi
    assert rendered.count('href="/papers/1"') == 1


def test_memory_detail_resolves_body_handles_once(runtime, client, monkeypatch) -> None:
    from precis_web.routes import refs

    body = "See me474326.\n\n| handle |\n| --- |\n| me474326 |"
    calls: list[list[str]] = []

    async def dispatch(request, verb, args):
        return body, False

    def resolve(store, tokens):
        assert store is runtime.store
        calls.append(list(tokens))
        return _HANDLES

    monkeypatch.setattr(refs, "await_dispatch", dispatch)
    monkeypatch.setattr(refs.mentions, "resolve_handle_tokens", resolve)

    response = client.get("/refs/memory/20")

    assert response.status_code == 200
    assert calls == [["me474326"]]
    assert response.text.count(_HREF) == 2
    assert "<table" in response.text
