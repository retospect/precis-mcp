"""Unit tests for the freedom-to-operate claims digest (slice 3).

See ``src/precis/workers/patent_digest.py`` and
``docs/design/patent-authoring-loop.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.workers.patent_digest import (
    build_claims_digest,
    discover_our_claim_handles,
    refresh_claims_digest,
    related_patent_ref_ids,
    stamp_claims_digest,
)


@dataclass
class _Block:
    id: int
    meta: dict[str, Any] = field(default_factory=dict)
    chunk_kind: str = "paragraph"


@dataclass
class _Ref:
    kind: str


@dataclass
class _Link:
    dst_ref_id: int


@dataclass
class _Chunk:
    dc: str
    chunk_kind: str = "paragraph"


class _FakeStore:
    def __init__(
        self,
        blocks_by_ref: dict[int, list[_Block]],
        *,
        links: dict[int, list[_Link]] | None = None,
        refs: dict[int, _Ref] | None = None,
        chunks: dict[int, list[_Chunk]] | None = None,
        styles: dict[str, str] | None = None,
    ) -> None:
        self._blocks = blocks_by_ref
        self._links = links or {}
        self._refs = refs or {}
        self._chunks = chunks or {}
        self._styles = styles or {}
        self.stamped: list[tuple[int, dict[str, Any]]] = []

    def list_blocks_for_ref(self, ref_id: int) -> list[_Block]:
        return self._blocks.get(ref_id, [])

    def stamp_ref_meta(self, ref_id: int, patch: dict[str, Any]) -> None:
        self.stamped.append((ref_id, patch))

    def links_for(self, ref_id: int, *, direction: str = "both") -> list[_Link]:
        return self._links.get(ref_id, [])

    def fetch_refs_by_ids(self, ids: list[int]) -> dict[int, _Ref]:
        return {i: self._refs[i] for i in ids if i in self._refs}

    def reading_order(self, ref_id: int) -> list[_Chunk]:
        return self._chunks.get(ref_id, [])

    def section_style_for(self, handle: str) -> str | None:
        return self._styles.get(handle)


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


def test_digest_selects_claims_by_tier_in_document_order() -> None:
    store = _FakeStore({70: _patent_blocks()})
    ws = build_claims_digest(store, [70])
    # Description block excluded; independent claim verbatim, dependent summary.
    assert ws["eyes"] == [
        {"handle": "pk11", "extent": "verbatim"},
        {"handle": "pk12", "extent": "summary"},
    ]
    assert ws["edit_hint"] == []


def test_claim_families_group_in_document_order() -> None:
    # A patent with two claim families: indep 1 + dep 2, then indep 3 + dep 4.
    blocks = [
        _Block(id=1, meta={"patent_block": "claim", "claim_independent": True}),
        _Block(id=2, meta={"patent_block": "claim", "claim_independent": False}),
        _Block(id=3, meta={"patent_block": "claim", "claim_independent": True}),
        _Block(id=4, meta={"patent_block": "claim", "claim_independent": False}),
    ]
    ws = build_claims_digest(_FakeStore({70: blocks}), [70])
    # Each independent claim is immediately followed by its dependent —
    # families stay together (not all-independents-then-all-dependents).
    assert ws["eyes"] == [
        {"handle": "pk1", "extent": "verbatim"},
        {"handle": "pk2", "extent": "summary"},
        {"handle": "pk3", "extent": "verbatim"},
        {"handle": "pk4", "extent": "summary"},
    ]


def test_google_sourced_claims_are_recognized_verbatim() -> None:
    # A CN patent whose body came from patents.google.com: one chunk per
    # claim, chunk_kind='patent_claim', NO patent_block meta. Each is a
    # claim eye, verbatim.
    blocks = [
        _Block(id=1, chunk_kind="patent_section"),  # description — excluded
        _Block(id=2, chunk_kind="patent_claim"),  # claim 1
        _Block(id=3, chunk_kind="patent_claim"),  # claim 2
    ]
    ws = build_claims_digest(_FakeStore({70: blocks}), [70])
    assert ws["eyes"] == [
        {"handle": "pk2", "extent": "verbatim"},
        {"handle": "pk3", "extent": "verbatim"},
    ]


def test_ops_and_google_claims_both_counted() -> None:
    # A ref carrying both marker schemes (an OPS claim + a google claim).
    blocks = [
        _Block(id=1, meta={"patent_block": "claim", "claim_independent": True}),
        _Block(id=2, chunk_kind="patent_claim"),
    ]
    ws = build_claims_digest(_FakeStore({70: blocks}), [70])
    assert [e["handle"] for e in ws["eyes"]] == ["pk1", "pk2"]


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
    ws = refresh_claims_digest(store, 999, 500, our_claim_handles=[])
    assert ws == {"eyes": [], "edit_hint": []}
    assert store.stamped == [(999, {"working_set": ws})]


def test_discover_our_claim_handles_finds_claim_section_leaves() -> None:
    # Draft 500: a claims heading + two claim leaves + a non-claim paragraph.
    store = _FakeStore(
        {},
        chunks={
            500: [
                _Chunk("dc10", chunk_kind="heading"),  # the "Claims" heading
                _Chunk("dc11"),  # claim 1 (under patent-claim)
                _Chunk("dc12"),  # claim 2 (under patent-claim)
                _Chunk("dc20"),  # a description paragraph (different section)
            ]
        },
        styles={
            "dc10": "patent-claim",  # heading itself — excluded (it's a heading)
            "dc11": "patent-claim",
            "dc12": "patent-claim",
            "dc20": "patent-description",
        },
    )
    assert discover_our_claim_handles(store, 500) == ["dc11", "dc12"]


def test_refresh_auto_includes_our_claims() -> None:
    store = _FakeStore(
        {70: _patent_blocks()},
        links={500: [_Link(70)]},
        refs={70: _Ref("patent")},
        chunks={500: [_Chunk("dc11"), _Chunk("dc12")]},
        styles={"dc11": "patent-claim", "dc12": "patent-claim"},
    )
    ws = refresh_claims_digest(store, 999, 500)  # no explicit our_claim_handles
    handles = [e["handle"] for e in ws["eyes"]]
    assert handles[:2] == ["dc11", "dc12"]  # our claims lead, verbatim
    assert "pk11" in handles  # prior-art claim also present
    assert ws["edit_hint"] == ["dc11", "dc12"]
