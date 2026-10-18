"""gr495190: cross-kind ranking — page_size reaches handlers, exact titles lead."""

from __future__ import annotations

from typing import Any

import pytest

from precis.runtime.core import PrecisRuntime
from precis.utils.search_merge import SearchHit, flag_title_matches, merge_and_render


def _hit(kind: str, title: str, ref_id: int, score: float = 0.5) -> SearchHit:
    return SearchHit(
        score=score, kind=kind, title=title, preview="p", ref_id=ref_id, slug=None
    )


def test_flag_title_matches_exact_phrase_and_word_boundary() -> None:
    hits = [
        _hit("todo", "Dogfood", 1),
        _hit("todo", "Dogfood run 3: results", 2),
        _hit("todo", "dogfooding notes", 3),
        _hit("todo", "unrelated", 4),
    ]
    out = flag_title_matches(hits, "  dogfood! ")
    assert [(h.pinned, h.title_phrase) for h in out] == [
        (True, True),
        (False, True),
        (False, False),
        (False, False),
    ]


def test_flag_title_matches_blank_query_is_noop() -> None:
    hits = [_hit("todo", "x", 1)]
    assert flag_title_matches(hits, "  ") == hits


def test_rrf_lifts_a_title_phrase_hit_over_other_streams_rank_one() -> None:
    semantic = [_hit("paper", f"neighbour {i}", 100 + i) for i in range(10)]
    small_kind = [_hit("todo", f"filler {i}", 200 + i) for i in range(4)]
    small_kind.append(_hit("todo", "Dogfood run 3", 204))
    streams = [semantic, flag_title_matches(small_kind, "dogfood")]
    body = merge_and_render(streams, page_size=20, mode="rrf", output_shape="toon").body
    assert body.index("Dogfood run 3") < body.index("neighbour 0")


def _rt_two_kinds(rt: PrecisRuntime) -> list[str]:
    kinds = rt._cross_kind_kinds()
    assert len(kinds) >= 2
    return kinds[:2]


def test_exact_title_hit_ranks_first_in_cross_kind_search(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    big, small = _rt_two_kinds(rt)
    semantic = [_hit(big, f"semantic neighbour {i}", 100 + i) for i in range(10)]
    lexical = [_hit(small, f"filler {i}", 200 + i) for i in range(6)]
    lexical.append(_hit(small, "Dogfood", 206))

    monkeypatch.setattr(
        type(rt),
        "_cross_kind_fan_out",
        lambda self, kinds, kw: {big: semantic, small: lexical},
    )
    monkeypatch.setattr(
        type(rt), "_resolve_cross_kind_request", lambda self, kind: [big, small]
    )
    resp = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert resp.body.index("Dogfood") < resp.body.index("semantic neighbour 0")


def test_page_size_reaches_every_handler_as_page_size(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime_with_store
    seen: list[dict[str, Any]] = []

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        seen.append(dict(kw))
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    rt._dispatch_cross_kind("*", {"q": "dogfood", "page_size": 37, "mode": "lexical"})
    assert seen
    assert all(kw["page_size"] == 37 and "top_k" not in kw for kw in seen)
    seen.clear()
    rt._dispatch_cross_kind("*", {"q": "dogfood", "top_k": 12, "mode": "lexical"})
    assert all(kw["page_size"] == 12 for kw in seen)
