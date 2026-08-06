""" "Who links here" backlinks panel (paper Meta tab).

Covers the two pure helpers behind the panel:

- ``papers._backlinks`` — reads the materialised ``links`` reverse index
  (incoming edges), groups by ``(source kind, relation)``, dedupes a source
  that links at multiple chunks into one row (counting the edges), and caps
  each group with an overflow note.
- ``papers._src_url`` — the kind-agnostic canonical URL for a linking source
  (shared ``_OPEN_URL_OVERRIDES`` map + the ``finding`` → ``/claim/fi<id>``
  override + the ``/refs/<kind>/<id>`` fallback).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

import precis_web.routes.papers as papers_routes


def _lk(src: int, dst: int, relation: str, *, src_pos: int | None = None) -> Any:
    return SimpleNamespace(
        src_ref_id=src, dst_ref_id=dst, relation=relation, src_pos=src_pos
    )


def _ref(
    id_: int, kind: str, *, slug: str = "", title: str = "", deleted_at: Any = None
) -> Any:
    return SimpleNamespace(
        id=id_, kind=kind, slug=slug, title=title, deleted_at=deleted_at
    )


class _FakeStore:
    """Minimal store for ``_backlinks``: an incoming-links list + a ref pool."""

    def __init__(self, links: list[Any], refs: list[Any]) -> None:
        self._links = links
        self._refs = {r.id: r for r in refs}

    def links_for(
        self, ref_id: int, *, direction: str = "both", relation: str | None = None
    ) -> list[Any]:
        assert direction == "in" and relation is None
        return [lk for lk in self._links if lk.dst_ref_id == ref_id]

    def fetch_refs_by_ids(
        self, ids: list[int], *, include_deleted: bool = False
    ) -> dict[int, Any]:
        return {i: self._refs[i] for i in ids if i in self._refs}


# ── _backlinks ──────────────────────────────────────────────────────────


def test_backlinks_groups_by_kind_and_relation() -> None:
    paper = 42553
    store = _FakeStore(
        links=[
            _lk(500, paper, "cites"),  # a draft cites it
            _lk(600, paper, "derived-from"),  # a finding derives from it
            _lk(11, paper, "cites"),  # a sibling paper cites it
        ],
        refs=[
            _ref(500, "draft", slug="nanobuds", title="Carbon Nanobuds review"),
            _ref(600, "finding", title="A grounded claim"),
            _ref(11, "paper", slug="jones2025", title="Another paper"),
        ],
    )
    groups = papers_routes._backlinks(store, paper)
    keys = {(g["kind"], g["relation"]) for g in groups}
    assert keys == {("draft", "cites"), ("finding", "derived-from"), ("paper", "cites")}
    by = {(g["kind"], g["relation"]): g for g in groups}
    # Each source links to its canonical page.
    assert by[("draft", "cites")]["rows"][0]["url"] == "/smartdraft/500"
    assert by[("finding", "derived-from")]["rows"][0]["url"] == "/claim/fi600"
    assert by[("paper", "cites")]["rows"][0]["url"] == "/papers/jones2025"


def test_backlinks_dedupes_source_across_chunks_and_counts_edges() -> None:
    paper = 42553
    store = _FakeStore(
        links=[
            _lk(500, paper, "cites", src_pos=3),
            _lk(500, paper, "cites", src_pos=9),
            _lk(500, paper, "cites", src_pos=14),
        ],
        refs=[_ref(500, "draft", slug="nanobuds", title="Nanobuds")],
    )
    (group,) = papers_routes._backlinks(store, paper)
    assert group["count"] == 1
    (row,) = group["rows"]
    assert row["edges"] == 3
    assert row["url"] == "/smartdraft/500"


def test_backlinks_skips_missing_and_deleted_sources() -> None:
    paper = 42553
    store = _FakeStore(
        links=[
            _lk(500, paper, "cites"),  # deleted → skipped
            _lk(999, paper, "cites"),  # not in pool → skipped
            _lk(600, paper, "cites"),  # live → kept
        ],
        refs=[
            _ref(500, "draft", title="Gone", deleted_at="2026-01-01"),
            _ref(600, "draft", slug="live", title="Live draft"),
        ],
    )
    (group,) = papers_routes._backlinks(store, paper)
    assert [r["url"] for r in group["rows"]] == ["/smartdraft/600"]


def test_backlinks_empty_returns_empty() -> None:
    store = _FakeStore(links=[], refs=[])
    assert papers_routes._backlinks(store, 42553) == []


def test_backlinks_group_overflow_caps_and_notes() -> None:
    paper = 42553
    n = papers_routes._BACKLINKS_GROUP_CAP + 5
    store = _FakeStore(
        links=[_lk(1000 + i, paper, "cites") for i in range(n)],
        refs=[
            _ref(1000 + i, "draft", slug=f"d{i}", title=f"Draft {i}") for i in range(n)
        ],
    )
    (group,) = papers_routes._backlinks(store, paper)
    assert group["count"] == n
    assert len(group["rows"]) == papers_routes._BACKLINKS_GROUP_CAP
    assert group["overflow"] == 5


def test_backlinks_ignores_self_link() -> None:
    paper = 42553
    store = _FakeStore(
        links=[_lk(paper, paper, "related-to")],
        refs=[_ref(paper, "paper", slug="self", title="Self")],
    )
    assert papers_routes._backlinks(store, paper) == []


# ── _src_url ────────────────────────────────────────────────────────────


def test_src_url_paper_prefers_slug() -> None:
    assert (
        papers_routes._src_url(_ref(11, "paper", slug="jones2025"))
        == "/papers/jones2025"
    )


def test_src_url_paper_falls_back_to_id() -> None:
    assert papers_routes._src_url(_ref(11, "paper")) == "/papers/11"


def test_src_url_draft_uses_smartdraft_by_id() -> None:
    assert (
        papers_routes._src_url(_ref(500, "draft", slug="nanobuds")) == "/smartdraft/500"
    )


def test_src_url_finding_opens_claim_page() -> None:
    assert papers_routes._src_url(_ref(600, "finding")) == "/claim/fi600"


def test_src_url_unknown_kind_uses_refs_fallback() -> None:
    assert papers_routes._src_url(_ref(700, "dream")) == "/refs/dream/700"
