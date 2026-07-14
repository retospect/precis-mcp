"""Dream fisheye pulls in papers cited by recently-active drafts.

The draft handler auto-materialises `cites` edges from `[pc<id>]` handles, so
the dream frontier can ride that existing graph — a wandering re-read of an
active draft sees the evidence it cites (drift/contradiction spotting, the
payoff named in docs/design/dreaming.md).
"""

from __future__ import annotations

from precis.workers.dream_agent import (
    _draft_cite_eye_count,
    _recent_draft_cited_paper_ids,
)


def _paper(store, slug: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=f"Paper {slug}", meta={}).id


def _draft(store, slug: str) -> int:
    return store.insert_ref(kind="draft", slug=slug, title=f"Draft {slug}", meta={}).id


def test_returns_papers_a_draft_cites(store) -> None:
    p1 = _paper(store, "cited-one")
    p2 = _paper(store, "cited-two")
    uncited = _paper(store, "uncited")
    d = _draft(store, "dcite")
    store.add_link(src_ref_id=d, dst_ref_id=p1, relation="cites")
    store.add_link(src_ref_id=d, dst_ref_id=p2, relation="cites")

    got = set(_recent_draft_cited_paper_ids(store, 10))
    assert {p1, p2} <= got
    assert uncited not in got


def test_non_cites_relation_excluded(store) -> None:
    p = _paper(store, "related-only")
    d = _draft(store, "drel")
    store.add_link(src_ref_id=d, dst_ref_id=p, relation="related-to")
    assert p not in set(_recent_draft_cited_paper_ids(store, 10))


def test_deleted_cited_paper_excluded(store) -> None:
    p = _paper(store, "gone")
    d = _draft(store, "ddel")
    store.add_link(src_ref_id=d, dst_ref_id=p, relation="cites")
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET deleted_at = now() WHERE ref_id = %s", (p,))
        conn.commit()
    assert p not in set(_recent_draft_cited_paper_ids(store, 10))


def test_limit_zero_short_circuits(store) -> None:
    assert _recent_draft_cited_paper_ids(store, 0) == []


def test_eye_count_env(monkeypatch) -> None:
    monkeypatch.delenv("PRECIS_DREAM_DRAFT_CITE_EYES", raising=False)
    assert _draft_cite_eye_count() == 2  # default
    monkeypatch.setenv("PRECIS_DREAM_DRAFT_CITE_EYES", "0")
    assert _draft_cite_eye_count() == 0  # disable
    monkeypatch.setenv("PRECIS_DREAM_DRAFT_CITE_EYES", "5")
    assert _draft_cite_eye_count() == 5
    monkeypatch.setenv("PRECIS_DREAM_DRAFT_CITE_EYES", "garbage")
    assert _draft_cite_eye_count() == 2  # falls back
