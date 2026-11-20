"""Word handles: reversible number <-> words map and the ``handle`` kind (gr648264, gr674040)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Denied, NotFound
from precis.handle_words import (
    _ADJECTIVES,
    _COLORS,
    _NOUNS,
    pair_residue,
    to_number,
    to_words,
    to_words2,
)
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
    pair = to_words2(648264)
    words = to_words(648264)
    # id -> words answers the two-word pair (three only on a live clash,
    # which a store-less hub cannot see).
    assert h.get(id=648264).body == f"648264 = {pair}\n"
    # 3+-word input resolves to the exact number in either slot.
    assert "648264" in h.get(q=words).body
    assert "648264" in h.get(id=words).body
    with pytest.raises(BadInput):
        h.get()
    with pytest.raises(BadInput):
        h.get(q="not a handle")
    # a pair needs a store to name which of its refs the caller meant.
    with pytest.raises(BadInput):
        h.get(q=pair)


def test_two_word_pair_matches_id_mod() -> None:
    # The gripe's evidence case: neighbouring ids share the three-word
    # prefix but the two-word form differs.
    assert to_words2(674030) == "gentle beacon"
    assert to_words2(674030) != to_words2(674040)
    for n in (0, 1, 127, 128, 16383, 16384, 648264, 2097151, 2097152, 10**9):
        # the pair is the last two words of the full form — id % 16384.
        assert to_words2(n) == " ".join(to_words(n).split()[-2:])
        assert pair_residue(to_words2(n)) == n % 16384


def test_handler_id_list() -> None:
    h = HandleHandler(hub=Hub())
    out = h.get(id=[674030, 674040])
    assert out.body == "674030 = gentle beacon\n674040 = gentle lizard\n"


def test_clash_returns_three_words(runtime_with_store: PrecisRuntime) -> None:
    """A second live ref on ``id % 16384`` → three words + a clash note."""
    body, error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": "clash probe ref"}
    )
    assert not error, body
    store = runtime_with_store.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'memory' AND title = %s",
            ("clash probe ref",),
        ).fetchone()
    assert row is not None
    ref_id = int(row[0])
    h = HandleHandler(hub=Hub(store=store))

    # The ref's own pair is unclaimed → two words.
    assert h.get(id=ref_id).body == f"{ref_id} = {to_words2(ref_id)}\n"

    # Another id sharing the residue → three-word form + a clash note.
    clashing = ref_id + 16384
    out = h.get(id=clashing).body
    assert f"{clashing} = {to_words(clashing)}" in out
    assert "clash" in out
    assert to_words2(clashing) in out

    # A batch holding a clash still answers one physical line per input,
    # the note on the clashing id's own line.
    batch = h.get(id=[ref_id, clashing, 674030]).body
    lines = batch.splitlines()
    assert len(lines) == 3
    assert lines[0] == f"{ref_id} = {to_words2(ref_id)}"
    assert lines[1].startswith(f"{clashing} = {to_words(clashing)}")
    assert "clash" in lines[1]
    assert lines[2].startswith("674030 = ")

    # The caller's read scope fences the probe: a domain without
    # ``memory`` neither names nor counts the clashing ref.
    from precis.runtime.scope import Domain, request_domain

    with request_domain(Domain(name="t", kinds=frozenset({"todo"}))):
        assert h.get(id=clashing).body == f"{clashing} = {to_words2(clashing)}\n"
        assert "names no live ref" in h.get(q=to_words2(ref_id)).body

    # q= on the pair returns the candidate list — the ref is in it.
    pair = to_words2(ref_id)
    out = h.get(q=pair).body
    assert pair in out
    assert str(ref_id) in out or "memory" in out
    # of_kind narrows the candidate list.
    assert "names no live ref" in h.get(q=pair, of_kind="todo").body


class _ResidueStore:
    """Store stand-in for the residue probes: a fixed ref set paged
    the way the real store does (newest-first ordering is the caller's
    list order here — the handler only reads limit/offset/count)."""

    def __init__(self, ids: list[int], kind: str = "memory") -> None:
        self._refs = [SimpleNamespace(id=i, kind=kind, title=f"ref {i}") for i in ids]

    def _pool(
        self,
        residue: int,
        modulus: int,
        kind: str | None,
        exclude_id: int | None,
    ) -> list[Any]:
        return [
            r
            for r in self._refs
            if r.id % modulus == residue
            and (kind is None or r.kind == kind)
            and r.id != exclude_id
        ]

    def refs_sharing_id_residue(
        self,
        residue: int,
        *,
        modulus: int,
        kind: str | None = None,
        exclude_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
        **_scope: Any,
    ) -> list[Any]:
        pool = self._pool(residue, modulus, kind, exclude_id)
        return pool[offset : offset + limit]

    def count_refs_sharing_id_residue(
        self,
        residue: int,
        *,
        modulus: int,
        kind: str | None = None,
        exclude_id: int | None = None,
        **_scope: Any,
    ) -> int:
        return len(self._pool(residue, modulus, kind, exclude_id))


def test_pair_candidates_paginate() -> None:
    """gr674040 review: >20 sharers must report the true total and page —
    a silent LIMIT 20 stranded the oldest candidate."""
    residue = pair_residue("gentle beacon")
    ids = [residue + 16384 * i for i in range(23)]
    h = HandleHandler(hub=Hub(store=cast(Store, _ResidueStore(ids))))

    page1 = h.get(q="gentle beacon")
    assert "names 23 live ref(s), page 1 of 2" in page1.body
    assert page1.result_count == 20
    assert "args={'page': 2, 'page_size': 20}" in page1.body

    page2 = h.get(q="gentle beacon", page=2)
    assert "page 2 of 2" in page2.body
    assert page2.result_count == 3
    # The tail of the list — unreachable under the old LIMIT — is here.
    assert str(ids[22]) not in page1.body
    assert str(ids[22]) in page2.body

    past = h.get(q="gentle beacon", page=5)
    assert "past the end" in past.body

    # id= clash notes count the other sharers accurately too.
    line = h.get(id=ids[0]).body
    assert "clash" in line
    assert "+18 more" in line  # 22 others, 4 named

    # page args only make sense on a pair listing.
    with pytest.raises(BadInput):
        h.get(id=ids[0], page=2)
    id_list: list[str | int] = list(ids[:2])
    with pytest.raises(BadInput):
        h.get(id=id_list, page=2)
    with pytest.raises(BadInput):
        h.get(q=to_words(ids[0]), page=2)  # a full three-word read
    with pytest.raises(BadInput):
        h.get(q="gentle beacon", page_size=0)
    with pytest.raises(BadInput):
        h.get(q="gentle beacon", page_size=101)


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


def test_pair_mcp_paging(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    residue = pair_residue("gentle beacon")
    ids = [residue + 16384 * i for i in range(5)]
    fake = _ResidueStore(ids)
    monkeypatch.setattr(
        runtime_with_store.live_store,
        "refs_sharing_id_residue",
        fake.refs_sharing_id_residue,
    )
    monkeypatch.setattr(
        runtime_with_store.live_store,
        "count_refs_sharing_id_residue",
        fake.count_refs_sharing_id_residue,
    )
    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    first = core.get(
        kind="handle", q="gentle beacon", args={"page_size": 2, "of_kind": "memory"}
    )
    assert "page 1 of 3" in first
    assert "args={'page': 2, 'page_size': 2, 'of_kind': 'memory'}" in first
    second = core.get(
        kind="handle",
        q="gentle beacon",
        args={"page": 2, "page_size": 2, "of_kind": "memory"},
    )
    assert "page 2 of 3" in second
    assert str(ids[2]) in second
    assert str(ids[2]) not in first
    empty = core.get(kind="handle", q="gentle beacon", args={"of_kind": "todo"})
    assert "names no live ref" in empty


def test_pair_credential_and_domain_scope(store: Store) -> None:
    from precis.runtime.scope import Domain, request_domain

    ref = store.insert_ref(kind="memory", slug=None, title="private collision title")
    h = HandleHandler(hub=Hub(store=store))
    pair = to_words2(ref.id)

    def gate(verb: str, payload: dict[str, Any]) -> None:
        if payload["kind"] != "handle":
            raise Denied("handle only", next="use a broader credential")

    token = _KIND_GATE.set(gate)
    try:
        with request_domain(Domain(name="test", kinds=frozenset({"memory", "handle"}))):
            assert "names no live ref" in h.get(q=pair, args={"of_kind": "memory"}).body
            clash = ref.id + 16384
            assert h.get(id=clash).body == f"{clash} = {pair}\n"
    finally:
        _KIND_GATE.reset(token)
    with request_domain(Domain(name="test", kinds=frozenset({"todo"}))):
        assert "names no live ref" in h.get(q=pair, args={"of_kind": "memory"}).body
    assert str(ref.id) in h.get(q=pair).body
