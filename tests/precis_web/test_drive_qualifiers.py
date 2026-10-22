"""drive-slash-command-line slice 2: bare-word qualifiers in the Drive query.

``is:stub``/``has:body`` map onto the ``paper_chunks`` selector,
``tag:<v>`` onto a ``tag=`` facet, ``since:<N><d|h|w>`` onto ``since=``
(now UTC minus N); an unrecognised qualifier word stays in the free text
and the page names it in one notice. A leading ``//`` escapes the parse —
``//foo`` searches the literal ``/foo``. ``/tag:x`` keeps its leading
slash and is never a qualifier.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from precis_web.routes import drive
from precis_web.routes.drive import _peel_qualifiers


class _FrozenDatetime(datetime):
    """``datetime.now()`` pinned — ``since:<N><unit>`` computes its bound
    off the route module's ``datetime`` (the test_doctor_tick_scheduler
    pattern)."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 21, 18, 30, 0, tzinfo=tz or UTC)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(drive, "datetime", _FrozenDatetime)
    return _FrozenDatetime.now(UTC)


# ── _peel_qualifiers (the grammar) ──────────────────────────────────


@pytest.mark.parametrize(
    ("text", "want_rest", "want_tags", "want_chunks", "want_unknown"),
    [
        ("is:stub", "", [], "without", []),
        ("has:body", "", [], "with", []),
        ("nano is:stub", "nano", [], "without", []),
        ("is:stub nano", "nano", [], "without", []),
        ("tag:PRIO:high nano", "nano", ["PRIO:high"], None, []),
        ("tag:a tag:b x", "x", ["a", "b"], None, []),
        ("is:foo nano", "is:foo nano", [], None, ["is:foo"]),
        ("has:chunks nano", "has:chunks nano", [], None, ["has:chunks"]),
        ("since:soon x", "since:soon x", [], None, ["since:soon"]),
        ("tag:", "tag:", [], None, ["tag:"]),
        # ``/tag:x`` is an address-shaped literal, not a qualifier —
        # qualifiers are bare words.
        ("/tag:x", "/tag:x", [], None, []),
        ("is:stub has:body", "", [], "with", []),  # last wins
        ("Is:Stub nano", "nano", [], "without", []),  # case-insensitive
    ],
)
def test_peel_qualifiers(text, want_rest, want_tags, want_chunks, want_unknown):
    rest, tags, chunks, _since, unknown = _peel_qualifiers(text)
    assert (rest, tags, chunks, unknown) == (
        want_rest,
        want_tags,
        want_chunks,
        want_unknown,
    )


@pytest.mark.parametrize(
    ("word", "want"),
    [
        ("since:7d", "2026-10-14T18:30:00+00:00"),
        ("since:24h", "2026-10-20T18:30:00+00:00"),
        ("since:2w", "2026-10-07T18:30:00+00:00"),
    ],
)
def test_peel_qualifiers_since_units(frozen_now, word, want):
    _rest, _tags, _chunks, since, _unknown = _peel_qualifiers(f"nano {word}")
    assert since == want


# ── route: qualifiers map onto the existing facets ──────────────────


def test_route_is_stub_maps_to_paper_chunks_without(client, runtime):
    """``/paper is:stub`` leaves no free text, so the browse runs with
    the body-availability selector at ``without``."""
    resp = client.get("/drive", params={"q": "/paper is:stub", "submitted": "1"})
    assert resp.status_code == 200
    assert runtime.store.recent_kinds == ["paper"]
    assert runtime.store.recent_has_chunks is False


def test_route_has_body_maps_to_paper_chunks_with(client, runtime):
    resp = client.get("/drive", params={"q": "/paper has:body", "submitted": "1"})
    assert resp.status_code == 200
    assert runtime.store.recent_kinds == ["paper"]
    assert runtime.store.recent_has_chunks is True


def test_route_qualifier_word_leaves_free_text(client, runtime):
    """``/paper is:stub nano`` still searches ``nano`` — the qualifier is
    gone from the free text."""
    resp = client.get("/drive", params={"q": "/paper is:stub nano", "submitted": "1"})
    assert resp.status_code == 200
    assert runtime.store.result_total_call["q"] == "nano"
    assert runtime.store.result_total_call["kinds"] == ["paper"]


def test_route_tag_qualifier_becomes_tag_facet(client, runtime):
    """``tag:PRIO:high`` splits on the first colon only — the filter is
    the tag ``PRIO:high``."""
    resp = client.get("/drive", params={"q": "tag:PRIO:high nano"})
    assert resp.status_code == 200
    assert runtime.store.result_total_call["q"] == "nano"
    assert runtime.store.result_total_call["tags"] == ["PRIO:high"]


def test_route_since_qualifier_sets_since_facet(client, runtime, frozen_now):
    """``since:7d`` → ``since=`` seven days before the frozen now (UTC)."""
    resp = client.get("/drive", params={"q": "since:7d nano"})
    assert resp.status_code == 200
    assert runtime.store.result_total_call["since"] == datetime(
        2026, 10, 14, 18, 30, 0, tzinfo=UTC
    )
    assert runtime.store.result_total_call["q"] == "nano"


def test_route_unrecognised_qualifier_stays_and_is_named(client, runtime):
    """``since:soon`` is qualifier-shaped but not a qualifier — it stays
    in the free text and the page notices."""
    resp = client.get("/drive", params={"q": "since:soon nano"})
    assert resp.status_code == 200
    assert runtime.store.result_total_call["q"] == "since:soon nano"
    assert "unrecognised qualifier" in resp.text
    assert "since:soon" in resp.text


# ── route: the ``//`` escape and ``/tag:x`` ─────────────────────────


def test_route_double_slash_searches_the_literal(client, runtime):
    """``//paper`` skips the kind peel — it searches ``/paper`` across
    the default kind set rather than narrowing to kind paper."""
    resp = client.get("/drive", params={"q": "//paper"})
    assert resp.status_code == 200
    assert runtime.store.result_total_call["q"] == "/paper"
    assert runtime.store.search_kinds != ["paper"]
    assert len(runtime.store.search_kinds) > 1


def test_route_slash_tag_is_not_a_qualifier(client, runtime):
    """``/tag:x`` keeps its leading slash — the ``tag:`` qualifier is a
    bare word only."""
    resp = client.get("/drive", params={"q": "/tag:x nano"})
    assert resp.status_code == 200
    assert runtime.store.result_total_call["q"] == "/tag:x nano"
    assert runtime.store.result_total_call["tags"] in (None, [])


def test_route_qualifier_only_query_stays_a_browse(client, runtime):
    """No leftover free text → the browse path runs (no chunk search),
    so ``is:stub`` alone filters the landing list."""
    resp = client.get("/drive", params={"q": "is:stub", "submitted": "1"})
    assert resp.status_code == 200
    assert runtime.store.recent_has_chunks is False
    assert runtime.store.result_total_call is None
