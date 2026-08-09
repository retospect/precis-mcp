"""Tests for the patent-family read-side helpers
(``_patent_family.py``, docs/backlog/patent-evidence-parity.md Phase 2).

Pure lookups over ``refs.meta['family_id']`` — refs are inserted directly
via ``store.insert_ref`` (no OPS fetch needed) since the helper doesn't
care how a patent ref got there.
"""

from __future__ import annotations

from precis.handlers._patent_family import family_members, family_representative
from precis.store import Store


def _insert_patent(
    store: Store,
    *,
    slug: str,
    family_id: str | None,
    publication_date: str | None = None,
    year: int | None = None,
) -> int:
    meta: dict = {}
    if family_id is not None:
        meta["family_id"] = family_id
    if publication_date is not None:
        meta["publication_date"] = publication_date
    ref = store.insert_ref(
        kind="patent",
        slug=slug,
        title=f"Patent {slug}",
        provider="epo_ops",
        meta=meta,
        year=year,
    )
    return ref.id


class TestFamilyMembers:
    def test_unknown_family_returns_empty(self, store: Store) -> None:
        assert family_members(store, "no-such-family") == []

    def test_falsy_family_id_returns_empty(self, store: Store) -> None:
        assert family_members(store, None) == []
        assert family_members(store, "") == []

    def test_only_matching_family_returned(self, store: Store) -> None:
        _insert_patent(
            store, slug="ep0000001a1", family_id="fam-a", publication_date="2020-01-01"
        )
        _insert_patent(
            store, slug="ep0000002a1", family_id="fam-b", publication_date="2020-01-01"
        )
        members = family_members(store, "fam-a")
        assert [m.slug for m in members] == ["ep0000001a1"]


class TestFamilyRepresentative:
    def test_unknown_family_is_none(self, store: Store) -> None:
        assert family_representative(store, "no-such-family") is None

    def test_single_member_returns_itself(self, store: Store) -> None:
        _insert_patent(
            store, slug="ep0000001a1", family_id="fam-a", publication_date="2020-01-01"
        )
        rep = family_representative(store, "fam-a")
        assert rep is not None
        assert rep.slug == "ep0000001a1"

    def test_earliest_publication_wins(self, store: Store) -> None:
        _insert_patent(
            store, slug="ep0000002a1", family_id="fam-a", publication_date="2021-06-01"
        )
        _insert_patent(
            store, slug="ep0000001a1", family_id="fam-a", publication_date="2019-03-15"
        )
        _insert_patent(
            store, slug="ep0000003a1", family_id="fam-a", publication_date="2020-01-01"
        )
        rep = family_representative(store, "fam-a")
        assert rep is not None
        assert rep.slug == "ep0000001a1"

    def test_slug_ascending_tiebreak_on_equal_dates(self, store: Store) -> None:
        _insert_patent(
            store, slug="ep0000009a1", family_id="fam-a", publication_date="2020-01-01"
        )
        _insert_patent(
            store, slug="ep0000002a1", family_id="fam-a", publication_date="2020-01-01"
        )
        rep = family_representative(store, "fam-a")
        assert rep is not None
        assert rep.slug == "ep0000002a1"

    def test_year_fallback_when_publication_date_missing(self, store: Store) -> None:
        # Legacy ref ingested before publication_date was reliably parsed:
        # falls back to refs.year, still beating an undated member.
        _insert_patent(store, slug="ep0000001a1", family_id="fam-a", year=2018)
        _insert_patent(store, slug="ep0000002a1", family_id="fam-a")  # no date, no year
        rep = family_representative(store, "fam-a")
        assert rep is not None
        assert rep.slug == "ep0000001a1"

    def test_dated_member_beats_same_year_undated_member(self, store: Store) -> None:
        # A finer-grained publication_date always wins over a same-year
        # year-only fallback record (the year-only record degrades to
        # "end of year" for comparison purposes).
        _insert_patent(
            store, slug="ep0000001a1", family_id="fam-a", publication_date="2020-03-01"
        )
        _insert_patent(store, slug="ep0000002a1", family_id="fam-a", year=2020)
        rep = family_representative(store, "fam-a")
        assert rep is not None
        assert rep.slug == "ep0000001a1"

    def test_undated_member_never_beats_a_dated_one(self, store: Store) -> None:
        _insert_patent(
            store, slug="ep0000001a1", family_id="fam-a", publication_date="2099-01-01"
        )
        _insert_patent(store, slug="ep0000002a1", family_id="fam-a")  # no date at all
        rep = family_representative(store, "fam-a")
        assert rep is not None
        assert rep.slug == "ep0000001a1"
