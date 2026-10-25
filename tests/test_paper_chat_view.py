"""``view='chat'`` — the deterministic scaffold for a paste-ready chat
note (Discord, Slack).

Per paper, three lines: line 1 a marked placeholder for the agent's
one-line key-novelty summary, line 2 the byline, line 3 the locator
URL — then the abstract below a ``---`` separator so the summary is
written from stored text, never invented. One store + ``PaperHandler``
end-to-end (same shape as test_paper_summaries_view).
"""

from __future__ import annotations

from typing import Any

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import Ref, Store
from precis.utils import handle_registry

_PLACEHOLDER = "<one-line key-novelty summary"


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def _seed(
    store: Store,
    slug: str,
    *,
    authors: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    identifiers: list[tuple[str, str, str]] | None = None,
) -> Ref:
    ref = store.insert_ref(
        kind="paper", slug=slug, title=slug, authors=authors, meta=meta
    )
    if identifiers:
        store.insert_ref_identifiers(ref.id, identifiers)
    return ref


def _note_lines(body: str) -> list[str]:
    """The three scaffold lines: placeholder, byline, locator."""
    marker = next(i for i, line in enumerate(body.splitlines()) if _PLACEHOLDER in line)
    return body.splitlines()[marker : marker + 3]


def _author(given: str, family: str) -> dict[str, Any]:
    return {"given": given, "family": family}


def test_chat_view_one_author_bare_name(store: Store) -> None:
    ref = _seed(
        store,
        "solo01",
        authors=[_author("Ada", "Lovelace")],
        meta={"abstract": "Analytical engine notes."},
        identifiers=[("doi", "10.1234/solo.1", "test")],
    )
    body = _handler(store).get(id=ref.slug, view="chat").body
    placeholder, byline, locator = _note_lines(body)
    assert placeholder.startswith(_PLACEHOLDER)
    assert byline == "Ada Lovelace"
    assert locator == "https://doi.org/10.1234/solo.1"
    assert "Analytical engine notes." in body.split("---", 1)[1]


def test_chat_view_two_authors_ampersand(store: Store) -> None:
    ref = _seed(
        store,
        "duo01",
        authors=[_author("Ada", "Lovelace"), _author("Charles", "Babbage")],
    )
    body = _handler(store).get(id=ref.slug, view="chat").body
    assert _note_lines(body)[1] == "Ada Lovelace & Charles Babbage"


def test_chat_view_many_authors_first_ellipsis_last(store: Store) -> None:
    ref = _seed(
        store,
        "crowd01",
        authors=[
            _author("Ada", "Lovelace"),
            _author("Charles", "Babbage"),
            _author("Grace", "Hopper"),
        ],
    )
    body = _handler(store).get(id=ref.slug, view="chat").body
    assert _note_lines(body)[1] == "Ada Lovelace … Grace Hopper"


def test_chat_view_missing_author_list(store: Store) -> None:
    ref = _seed(store, "anon01")
    body = _handler(store).get(id=ref.slug, view="chat").body
    assert _note_lines(body)[1] == "no authors on file"


def test_chat_view_arxiv_fallback_and_no_doi(store: Store) -> None:
    ref = _seed(
        store,
        "preprint01",
        identifiers=[("arxiv", "2401.00001", "test")],
    )
    body = _handler(store).get(id=ref.slug, view="chat").body
    assert _note_lines(body)[2] == "https://arxiv.org/abs/2401.00001"


def test_chat_view_publisher_url_then_no_doi(store: Store) -> None:
    ref = _seed(
        store,
        "puburl01",
        meta={"url": "https://publisher.example/article/42"},
    )
    body = _handler(store).get(id=ref.slug, view="chat").body
    assert _note_lines(body)[2] == "https://publisher.example/article/42"

    bare = _seed(store, "bare01")
    body = _handler(store).get(id=bare.slug, view="chat").body
    assert _note_lines(body)[2] == "no DOI"


def test_chat_view_doi_wins_over_arxiv(store: Store) -> None:
    ref = _seed(
        store,
        "both01",
        identifiers=[
            ("doi", "10.1234/both.1", "test"),
            ("arxiv", "2401.00002", "test"),
        ],
    )
    body = _handler(store).get(id=ref.slug, view="chat").body
    assert _note_lines(body)[2] == "https://doi.org/10.1234/both.1"


def test_chat_view_path_form_matches_kwarg(store: Store) -> None:
    ref = _seed(store, "path01", authors=[_author("Ada", "Lovelace")])
    handler = _handler(store)
    assert (
        handler.get(id=ref.slug, view="chat").body
        == handler.get(id=f"{ref.slug}/chat").body
    )


def test_chat_view_batch_renders_one_block_per_id_in_order(store: Store) -> None:
    first = _seed(store, "b1", authors=[_author("Ada", "Lovelace")])
    second = _seed(store, "b2", authors=[_author("Grace", "Hopper")])
    pa1 = handle_registry.format_handle("paper", first.id)
    pa2 = handle_registry.format_handle("paper", second.id)
    body = _handler(store).get(id=[pa1, pa2], view="chat").body
    one = body.index("Ada Lovelace")
    two = body.index("Grace Hopper")
    assert one != -1 and two != -1 and one < two
    assert body.count(_PLACEHOLDER) == 2


def test_chat_view_is_advertised(store: Store) -> None:
    assert "chat" in _handler(store).accepted_views()
