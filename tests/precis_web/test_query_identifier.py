"""DOI-prefix store lookup + the identifier resolver behind the /drive
search-box redirect (real PG via the ``store`` fixture)."""

from __future__ import annotations

import pytest

from precis.handlers._query_identifier import resolve_query_identifier


def _ref_id(store, q: str) -> int | None:
    ref = resolve_query_identifier(store, q).ref
    return None if ref is None else int(ref.id)


def _paper(store, slug: str, doi: str):
    ref = store.insert_ref(kind="paper", slug=slug, title=slug, meta={})
    store.insert_ref_identifiers(ref.id, [("doi", doi, "manual")])
    return ref


def test_prefix_unique(store):
    a = _paper(store, "alpha2020x", "10.1021/acscatal.3c01963")
    _paper(store, "beta2020x", "10.1021/jacs.3c00001")
    assert store.find_paper_ref_ids_by_doi_prefix("10.1021/acscatal.3c0196") == [a.id]
    # URL wrapper and case are normalised like a full DOI.
    assert store.find_paper_ref_ids_by_doi_prefix(
        "https://doi.org/10.1021/ACSCATAL.3c0196"
    ) == [a.id]


def test_prefix_ambiguous_and_limit(store):
    a = _paper(store, "alpha2020x", "10.1021/acscatal.3c01963")
    b = _paper(store, "beta2020x", "10.1021/acscatal.3c01964")
    assert store.find_paper_ref_ids_by_doi_prefix("10.1021/acscatal.3c0196") == [
        a.id,
        b.id,
    ]
    assert len(store.find_paper_ref_ids_by_doi_prefix("10.1021/", limit=1)) == 1


def test_prefix_wildcards_are_literal(store):
    _paper(store, "alpha2020x", "10.1021/acscatal.3c01963")
    assert store.find_paper_ref_ids_by_doi_prefix("10.1021/%") == []
    assert store.find_paper_ref_ids_by_doi_prefix("10.1021/acscatal_3c0196") == []
    assert store.find_paper_ref_ids_by_doi_prefix("10.1021/\\") == []
    assert store.find_paper_ref_ids_by_doi_prefix("") == []


def test_prefix_wildcard_matches_literal_underscore(store):
    u = _paper(store, "under2020x", "10.1000/a_b.1")
    _paper(store, "other2020x", "10.1000/axb.2")
    assert store.find_paper_ref_ids_by_doi_prefix("10.1000/a_b") == [u.id]


def test_prefix_excludes_retired(store):
    a = _paper(store, "alpha2020x", "10.1021/acscatal.3c01963")
    store.retire_ref(a.id)
    assert store.find_paper_ref_ids_by_doi_prefix("10.1021/acscatal") == []


def test_resolver_doi_prefix_and_ambiguity(store):
    a = _paper(store, "alpha2020x", "10.1021/acscatal.3c01963")
    assert _ref_id(store, "10.1021/acscatal.3c01963") == a.id
    assert _ref_id(store, " 10.1021/acscatal.3c0196 ") == a.id
    b = _paper(store, "beta2020x", "10.1021/acscatal.3c01964")
    m = resolve_query_identifier(store, "10.1021/acscatal.3c0196")
    assert m.ref is None and m.ambiguous_ref_ids == [a.id, b.id]
    assert resolve_query_identifier(store, "10.9999/nothing").ref is None


def test_resolver_handles(store):
    a = _paper(store, "alpha2020x", "10.1021/acscatal.3c01963")
    assert _ref_id(store, f"  pa{a.id} ") == a.id
    assert _ref_id(store, f"PA{a.id}") == a.id
    assert resolve_query_identifier(store, "pa99999999").ref is None
    assert resolve_query_identifier(store, "catalysis").ref is None
    assert resolve_query_identifier(store, "").ref is None


@pytest.mark.parametrize("q", ["10.", "10.1021", "pa", "pa1 pa2"])
def test_resolver_rejects_non_identifiers(store, q):
    assert resolve_query_identifier(store, q).ref is None
