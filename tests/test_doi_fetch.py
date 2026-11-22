"""``precis.ingest.doi_fetch`` — phase 2 of the DOI-completeness check
(gr690484): resolve a fetchable cite's DOI and write it back."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from precis.errors import BadInput
from precis.ingest import doi_fetch as D
from tests._fakes import FakeStore


class FakeRef:
    def __init__(
        self,
        title: str = "Deep Widgets for Science",
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.id = 1
        self.kind = "paper"
        self.title = title
        self.year = 2020
        self.authors = [{"family": "Smith", "given": "Jo"}]
        self.meta = meta or {}


class Store(FakeStore):
    def __init__(self, ref: FakeRef, ids: dict[str, str]) -> None:
        super().__init__()
        self.ref, self.ids = ref, ids
        self.written: list[tuple] = []
        self.patches: list[dict] = []

    def fetch_refs_by_ids(
        self, ids: Any, include_deleted: bool = False
    ) -> dict[int, FakeRef]:
        return {1: self.ref}

    def identifiers_for_refs(self, ids: Any) -> dict[int, dict[str, str]]:
        return {1: self.ids}

    def set_ref_identifier(
        self,
        ref_id: int,
        scheme: str,
        value: str,
        *,
        source: str = "x",
        conn: Any = None,
        only_if_missing: bool = False,
    ) -> bool:
        assert only_if_missing
        self.written.append((ref_id, scheme, value))
        return True

    def update_paper_fields(
        self,
        ref_id: int,
        *,
        meta_patch: dict[str, Any] | None = None,
        source: str = "x",
        **kw: Any,
    ) -> None:
        self.patches.append(meta_patch or {})
        self.ref.meta.update(meta_patch or {})


def _no(*a: Any) -> Any:
    raise AssertionError("network should not be touched")


def test_arxiv_only_cite_resolves_via_identifier_and_writes_back() -> None:
    store: Any = Store(FakeRef(), {"arxiv": "2001.00001"})
    out = D.fetch_missing_doi(
        store,
        1,
        identifier_fn=lambda i, e: {"doi": "https://doi.org/10.1234/ABC"},
        search_fn=_no,
    )
    assert out.outcome == "resolved"
    assert store.written == [(1, "doi", "10.1234/abc")]
    assert D.FETCH_ATTEMPTED_KEY in store.patches[0]


def test_title_search_accepts_close_match_only() -> None:
    good = {
        "doi": "10.1/ok",
        "title": "Deep widgets for science",
        "first_author": "Smith",
        "year": 2021,
    }
    store: Any = Store(FakeRef(), {"s2": "abc"})
    out = D.fetch_missing_doi(store, 1, search_fn=lambda *a: [good])
    assert out.outcome == "resolved" and out.doi == "10.1/ok"

    wrong = dict(good, first_author="Jones")
    store = Store(FakeRef(), {"s2": "abc"})
    out = D.fetch_missing_doi(store, 1, search_fn=lambda *a: [wrong])
    assert out.outcome == "unresolved" and store.written == []


def test_no_identifier_cite_is_left_alone() -> None:
    store: Any = Store(FakeRef(), {})
    out = D.fetch_missing_doi(store, 1, identifier_fn=_no, search_fn=_no)
    assert out.outcome == "no_identifier" and store.written == []


def test_existing_doi_untouched() -> None:
    store: Any = Store(FakeRef(), {"doi": "10.1/x", "arxiv": "1"})
    assert D.fetch_missing_doi(store, 1, search_fn=_no).outcome == "has_doi"


def test_ttl_gate_and_force() -> None:
    ref = FakeRef(meta={D.FETCH_ATTEMPTED_KEY: datetime.now(UTC).isoformat()})
    store: Any = Store(ref, {"arxiv": "1"})
    assert (
        D.fetch_missing_doi(store, 1, identifier_fn=_no, search_fn=_no).outcome
        == "fresh"
    )
    out = D.fetch_missing_doi(
        store, 1, force=True, identifier_fn=lambda i, e: None, search_fn=lambda *a: []
    )
    assert out.outcome == "unresolved"


def test_unresolved_thesis_carries_entry_type_note() -> None:
    store: Any = Store(FakeRef(meta={"entry_type": "dissertation"}), {"s2": "x"})
    out = D.fetch_missing_doi(store, 1, search_fn=lambda *a: [])
    assert out.outcome == "unresolved" and "Author decides" in (out.note or "")


def test_transport_failure_is_not_stamped() -> None:
    def boom(*a: Any) -> Any:
        raise RuntimeError("down")

    store: Any = Store(FakeRef(), {"arxiv": "1"})
    out = D.fetch_missing_doi(store, 1, identifier_fn=boom, search_fn=boom)
    assert out.outcome == "unchecked" and store.patches == []


@pytest.mark.parametrize("duplicate", [False, True])
def test_conclusive_write_refusal_is_ttl_gated(
    monkeypatch: pytest.MonkeyPatch, duplicate: bool
) -> None:
    store: Any = Store(FakeRef(), {"pubmed": "12345678"})

    def refuse(*a: Any, **kw: Any) -> bool:
        if duplicate:
            raise BadInput("DOI already held")
        return False

    def lookup(identifier: str, email: str) -> dict[str, str]:
        assert identifier == "pmid:12345678"
        return {"doi": "10.1234/held"}

    monkeypatch.setattr(store, "set_ref_identifier", refuse)
    result = D.fetch_missing_doi(store, 1, identifier_fn=lookup, search_fn=_no)
    assert result.outcome == "unresolved"
    assert result.error == ("DOI already held" if duplicate else "not written")
    assert D.fetch_missing_doi(store, 1, identifier_fn=_no).outcome == "fresh"
