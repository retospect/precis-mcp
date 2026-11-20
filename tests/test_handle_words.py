"""Word handles: reversible number <-> words map and the ``handle`` kind (gr648264)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Denied, NotFound
from precis.handle_words import _ADJECTIVES, _COLORS, _NOUNS, to_number, to_words
from precis.handlers.handle import HandleHandler
from precis.runtime import PrecisRuntime
from precis.runtime.dispatch import _KIND_GATE
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.tools import core


def test_lists_distinct() -> None:
    allw = _COLORS + _ADJECTIVES + _NOUNS
    assert len(allw) == len(set(allw)) == 384


@pytest.mark.parametrize(
    "n", [0, 1, 127, 128, 16383, 16384, 648264, 2097151, 2097152, 10**9]
)
def test_roundtrip(n: int) -> None:
    assert to_number(to_words(n)) == n


def test_known_and_distinct() -> None:
    assert to_words(0).split() == [_COLORS[0], _ADJECTIVES[0], _NOUNS[0]]
    assert len(to_words(648264).split()) == 3
    assert len({to_words(i) for i in range(5000)}) == 5000
    assert len(to_words(2097152).split()) == 4


def test_parse_tolerant_and_strict() -> None:
    w = to_words(648264)
    assert to_number(w.upper().replace(" ", "-")) == 648264
    with pytest.raises(ValueError):
        to_number("amber brave")
    with pytest.raises(ValueError):
        to_number("zzz brave dog")
    with pytest.raises(ValueError):
        to_number(f"{_COLORS[0]} {to_words(5)}")


def test_handler_both_directions() -> None:
    h = HandleHandler(hub=Hub())
    words = to_words(648264)
    assert words in h.get(id=648264).body
    assert "648264" in h.get(q=words).body
    assert "648264" in h.get(id=words).body
    with pytest.raises(BadInput):
        h.get()
    with pytest.raises(BadInput):
        h.get(q="not a handle")


def test_spoken_name_resolver(
    store: Store, runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [
        store.insert_ref(
            kind="paper",
            slug=f"miller-{year}",
            title=topic,
            meta={"author": "Miller, J.", "year": year, "title": topic},
        )
        for year, topic in [(2023, "Ammonia synthesis"), (2024, "Catalyst design")]
    ]
    for ref in refs:
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=f"The Miller paper on {ref.title}.")],
        )
    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    body = core.get(kind="handle", q="the Miller paper", args={"kind": "paper"})
    assert sum(line.startswith("pa") for line in body.splitlines()) == 2
    for ref, name in zip(
        refs, ["Miller 2023 ammonia", "Miller 2024 catalyst"], strict=True
    ):
        assert f"pa{ref.id} | {name} | {ref.title}" in body
    with pytest.raises(NotFound, match="search\\(kind='paper'"):
        HandleHandler(hub=Hub(store=store)).get(
            q="xylophoniczz", args={"kind": "paper"}
        )


def test_resolver_checks_target_scope() -> None:
    def refuse(verb: str, payload: dict[str, object]) -> None:
        assert verb == "search"
        assert payload["kind"] == "paper"
        raise Denied("paper scope required", next="use a paper read credential")

    token = _KIND_GATE.set(refuse)
    try:
        with pytest.raises(Denied):
            HandleHandler(hub=Hub()).get(q="Miller ammonia", args={"kind": "paper"})
    finally:
        _KIND_GATE.reset(token)
