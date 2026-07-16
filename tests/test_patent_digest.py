"""Unit tests for the freedom-to-operate claims digest (slice 3).

See ``src/precis/workers/patent_digest.py`` and
``docs/design/patent-authoring-loop.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.workers.patent_digest import (
    build_claims_digest,
    refresh_claims_digest,
    related_patent_ref_ids,
    stamp_claims_digest,
)


@dataclass
class _Block:
    id: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Ref:
    kind: str


@dataclass
class _Link:
    dst_ref_id: int


class _FakeStore:
    def __init__(
        self,
        blocks_by_ref: dict[int, list[_Block]],
        *,
        links: dict[int, list[_Link]] | None = None,
        refs: dict[int, _Ref] | None = None,
    ) -> None:
        self._blocks = blocks_by_ref
        self._links = links or {}
        self._refs = refs or {}
        self.stamped: list[tuple[int, dict[str, Any]]] = []

    def list_blocks_for_ref(self, ref_id: int) -> list[_Block]:
        return self._blocks.get(ref_id, [])

    def stamp_ref_meta(self, ref_id: int, patch: dict[str, Any]) -> None:
        self.stamped.append((ref_id, patch))

    def links_for(self, ref_id: int, *, direction: str = "both") -> list[_Link]:
        return self._links.get(ref_id, [])

    def fetch_refs_by_ids(self, ids: list[int]) -> dict[int, _Ref]:
        return {i: self._refs[i] for i in ids if i in self._refs}


def _patent_blocks() -> list[_Block]:
    # A tiny patent: 1 description block, claim 1 (independent), claim 2 (dep).
    return [
        _Block(id=10, meta={"patent_block": "description"}),
        _Block(
            id=11,
            meta={
                "patent_block": "claim",
                "claim_number": 1,
                "claim_independent": True,
                "depends_on": [],
            },
        ),
        _Block(
            id=12,
            meta={
                "patent_block": "claim",
                "claim_number": 2,
                "claim_independent": False,
                "depends_on": [1],
            },
        ),
    ]


def test_digest_selects_claims_independents_first_verbatim() -> None:
    store = _FakeStore({70: _patent_blocks()})
    ws = build_claims_digest(store, [70])
    # Description block excluded; independent claim verbatim, dependent summary.
    assert ws["eyes"] == [
        {"handle": "pk11", "extent": "verbatim"},
        {"handle": "pk12", "extent": "summary"},
    ]
    assert ws["edit_hint"] == []


def test_our_claims_lead_and_are_verbatim() -> None:
    store = _FakeStore({70: _patent_blocks()})
    ws = build_claims_digest(store, [70], our_claim_handles=["dc501", "dc502"])
    assert ws["eyes"][0] == {"handle": "dc501", "extent": "verbatim"}
    assert ws["eyes"][1] == {"handle": "dc502", "extent": "verbatim"}
    assert ws["edit_hint"] == ["dc501", "dc502"]


def test_dedup_first_extent_wins() -> None:
    # If our-claim list somehow repeats a prior-art handle, the first
    # (verbatim) extent is kept.
    store = _FakeStore({70: _patent_blocks()})
    ws = build_claims_digest(store, [70], our_claim_handles=["pk12"])
    handles = [e["handle"] for e in ws["eyes"]]
    assert handles.count("pk12") == 1
    assert ws["eyes"][0] == {"handle": "pk12", "extent": "verbatim"}


def test_multiple_patents_all_independents_present() -> None:
    store = _FakeStore({70: _patent_blocks(), 71: _patent_blocks()})
    ws = build_claims_digest(store, [70, 71])
    verbatim = [e["handle"] for e in ws["eyes"] if e["extent"] == "verbatim"]
    # Both patents' independent claims (pk11 from each ref — same chunk ids in
    # this fake, deduped) are present; never dropped.
    assert "pk11" in verbatim


def test_stamp_writes_working_set_meta() -> None:
    store = _FakeStore({70: _patent_blocks()})
    ws = stamp_claims_digest(store, 999, [70], our_claim_handles=["dc1"])
    assert store.stamped == [(999, {"working_set": ws})]
    assert ws["eyes"][0]["handle"] == "dc1"


def test_related_patents_filters_to_patent_kind() -> None:
    # Draft 500 links to patent 70, paper 71, and back to itself.
    store = _FakeStore(
        {70: _patent_blocks()},
        links={500: [_Link(70), _Link(71), _Link(500)]},
        refs={70: _Ref("patent"), 71: _Ref("paper"), 500: _Ref("draft")},
    )
    assert related_patent_ref_ids(store, 500) == [70]


def test_refresh_discovers_and_stamps() -> None:
    store = _FakeStore(
        {70: _patent_blocks()},
        links={500: [_Link(70), _Link(71)]},
        refs={70: _Ref("patent"), 71: _Ref("paper")},
    )
    ws = refresh_claims_digest(store, 999, 500, our_claim_handles=["dc9"])
    assert store.stamped == [(999, {"working_set": ws})]
    handles = [e["handle"] for e in ws["eyes"]]
    assert "dc9" in handles and "pk11" in handles  # our claim + prior-art claim


def test_refresh_no_patents_is_empty_but_safe() -> None:
    store = _FakeStore({}, links={500: []}, refs={})
    ws = refresh_claims_digest(store, 999, 500)
    assert ws == {"eyes": [], "edit_hint": []}
    assert store.stamped == [(999, {"working_set": ws})]
