"""Field-scoped paper lookup — ``search(kind='paper', title=…/author=…)``.

The block-hit path buries an exact-title query below content-dense
bodies and, for a bare author name, surfaces *other* papers' bibliography
lines instead of the paper itself (the combined card dilutes the byline).
``title=`` / ``author=`` match ``refs.title`` (trigram + FTS) /
``refs.authors`` (jsonb) directly and return paper *records* — the
handle + a one-line citation + a cite path — with held copies first.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import Store


def _seed_paper(
    store: Store,
    *,
    slug: str,
    title: str,
    authors: list[str],
    year: int | None = None,
) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=[{"name": n} for n in authors],
        year=year,
    )
    return ref.id


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store))


# ── store level ───────────────────────────────────────────────────


def test_find_papers_by_title_exact(store: Store) -> None:
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
    )
    _seed_paper(store, slug="other", title="A study of graphene oxide", authors=["X"])
    assert store.find_papers_by_title(kind="paper", q="attention is all you need") == [
        rid
    ]


def test_find_papers_by_title_partial(store: Store) -> None:
    """A few distinctive title words (FTS leg) still land the paper."""
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
    )
    assert rid in store.find_papers_by_title(kind="paper", q="attention need")


def test_find_papers_by_author_surname(store: Store) -> None:
    """A bare surname matches the structured byline (substring)."""
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
    )
    _seed_paper(store, slug="other", title="Unrelated", authors=["Jane Doe"])
    assert store.find_papers_by_author(kind="paper", q="Vaswani") == [rid]


def test_find_papers_by_author_no_match(store: Store) -> None:
    _seed_paper(store, slug="attn", title="T", authors=["Ashish Vaswani"])
    assert store.find_papers_by_author(kind="paper", q="Hinton") == []


# ── handler level ─────────────────────────────────────────────────


def test_handler_title_returns_record_row(store: Store) -> None:
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
    )
    out = _handler(store).search(title="attention is all you need")
    assert f"pa{rid}" in out.body
    assert "Vaswani" in out.body  # citation line
    assert "2017" in out.body
    assert "view='bibtex'" in out.body  # cite affordance


def test_handler_author_returns_record_row(store: Store) -> None:
    rid = _seed_paper(
        store,
        slug="attn",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
        year=2017,
    )
    out = _handler(store).search(author="Vaswani")
    assert f"pa{rid}" in out.body
    assert "Attention Is All You Need" in out.body


def test_handler_title_miss_suggests_stub(store: Store) -> None:
    out = _handler(store).search(title="a paper we do not hold")
    assert "no paper matches" in out.body
    assert "put(kind='paper'" in out.body  # request-it affordance


def test_handler_title_and_author_together_rejected(store: Store) -> None:
    with pytest.raises(BadInput):
        _handler(store).search(title="x", author="y")


def test_handler_empty_byline_falls_through_to_q_guard(store: Store) -> None:
    """Blank title/author is treated as absent — the normal q= guard fires."""
    with pytest.raises(BadInput):
        _handler(store).search(title="   ")
