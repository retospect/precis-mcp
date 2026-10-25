"""ingest.doi_registry — Crossref/DataCite DOI fallback (gr486090).

All offline: ``safe_get`` is patched with a canned-response stub, which
also proves the fetch path routes through the SSRF guard rather than a
bare ``client.get``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.ingest import doi_registry
from precis.ingest.doi_registry import RegistryLookupError, resolve_registry_doi


def _resp(status: int, payload: Any = None) -> Any:
    return SimpleNamespace(
        status_code=status,
        json=lambda: payload if payload is not None else {},
    )


def _patch_safe_get(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Stub ``safe_get`` to pop ``responses`` in order; returns the call log."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def _fake(client: Any, url: str, /, **kwargs: Any) -> Any:
        seen.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(doi_registry, "safe_get", _fake)
    return seen


_CROSSREF_HIT = {
    "status": "ok",
    "message": {
        "DOI": "10.1126/sciadv.adx3969",
        "title": ["A Real Science Advances Paper"],
        "author": [{"given": "Jane", "family": "Doe"}],
        "published-online": {"date-parts": [[2026]]},
        "container-title": ["Science Advances"],
        "type": "journal-article",
    },
}

_DATACITE_HIT = {
    "data": {
        "type": "dois",
        "attributes": {
            "doi": "10.4230/lipics.dna.32.9",
            "titles": [{"title": "A LIPIcs Proceedings Paper"}],
            "creators": [
                {
                    "givenName": "Jane",
                    "familyName": "Doe",
                    "nameType": "Personal",
                },
                {"name": "Some Consortium", "nameType": "Organizational"},
            ],
            "publicationYear": 2025,
            "publisher": "Schloss Dagstuhl",
            "container": {"title": "LIPIcs, Vol. 32", "type": "Series"},
            "descriptions": [
                {"description": "We prove things.", "descriptionType": "Abstract"}
            ],
        },
    }
}


def test_crossref_hit_normalizes_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_safe_get(monkeypatch, [_resp(200, _CROSSREF_HIT)])
    meta = resolve_registry_doi("10.1126/sciadv.adx3969")
    assert meta is not None
    assert meta["source"] == "crossref"
    assert meta["title"] == "A Real Science Advances Paper"
    assert meta["year"] == 2026
    assert meta["journal"] == "Science Advances"
    assert meta["authors"] == [{"given": "Jane", "family": "Doe"}]
    # The DOI rides the URL path URL-quoted, and the Crossref hit means
    # the DataCite leg never fires.
    assert seen == [
        (
            "https://api.crossref.org/works/10.1126%2Fsciadv.adx3969",
            {"params": {}},
        )
    ]


def test_crossref_miss_falls_through_to_datacite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_safe_get(monkeypatch, [_resp(404), _resp(200, _DATACITE_HIT)])
    meta = resolve_registry_doi("10.4230/lipics.dna.32.9")
    assert meta is not None
    assert meta["source"] == "datacite"
    assert meta["title"] == "A LIPIcs Proceedings Paper"
    assert meta["year"] == 2025
    assert meta["journal"] == "LIPIcs, Vol. 32"
    assert meta["abstract"] == "We prove things."
    assert meta["authors"] == [
        {"given": "Jane", "family": "Doe"},
        {"name": "Some Consortium"},
    ]
    assert [url for url, _ in seen] == [
        "https://api.crossref.org/works/10.4230%2Flipics.dna.32.9",
        "https://api.datacite.org/dois/10.4230%2Flipics.dna.32.9",
    ]


def test_all_registries_miss_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_safe_get(monkeypatch, [_resp(404), _resp(404)])
    assert resolve_registry_doi("10.fake/nope") is None
    assert len(seen) == 2


def test_non_404_status_is_a_fault_not_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Crossref 503 + DataCite 404: one leg faulted without a hit, so the
    # caller must see a fault (mint unverified), not "no such DOI".
    _patch_safe_get(monkeypatch, [_resp(503), _resp(404)])
    with pytest.raises(RegistryLookupError):
        resolve_registry_doi("10.1/whatever")


def test_transport_error_is_a_fault_not_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(client: Any, url: str, /, **kwargs: Any) -> Any:
        raise OSError("connection reset")

    monkeypatch.setattr(doi_registry, "safe_get", _boom)
    with pytest.raises(RegistryLookupError):
        resolve_registry_doi("10.1/whatever")


def test_fault_in_first_leg_does_not_mask_second_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _flaky(client: Any, url: str, /, **kwargs: Any) -> Any:
        calls.append(url)
        if "crossref" in url:
            raise OSError("crossref unreachable")
        return _resp(200, _DATACITE_HIT)

    monkeypatch.setattr(doi_registry, "safe_get", _flaky)
    meta = resolve_registry_doi("10.4230/lipics.dna.32.9")
    assert meta is not None and meta["source"] == "datacite"
    assert len(calls) == 2


# ── gr514384: whole-record cite typing (software / dataset records) ────


def _datacite_attrs(**overrides: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "doi": "10.5281/zenodo.22863181",
        "titles": [{"title": "precis-mcp"}],
        "creators": [{"name": "Stamm, Reto", "nameType": "Personal"}],
        "publicationYear": 2026,
        "publisher": "Zenodo",
        "version": "1.2.3",
    }
    attrs.update(overrides)
    return attrs


def test_datacite_software_typed_record() -> None:
    """``resourceTypeGeneral='Software'`` → ``record_type='software'`` +
    version/publisher; ``journal`` stays empty (the publisher is no
    longer relabelled a venue)."""
    meta = doi_registry._normalize_datacite(
        _datacite_attrs(types={"resourceTypeGeneral": "Software"}),
        "10.5281/zenodo.22863181",
    )
    assert meta["record_type"] == "software"
    assert meta["entry_type"] == "software"
    assert meta["version"] == "1.2.3"
    assert meta["publisher"] == "Zenodo"
    assert meta["journal"] == ""


def test_datacite_dataset_typed_record() -> None:
    meta = doi_registry._normalize_datacite(
        _datacite_attrs(types={"resourceTypeGeneral": "Dataset"}),
        "10.5281/zenodo.22863181",
    )
    assert meta["record_type"] == "dataset"
    assert meta["entry_type"] == "dataset"
    assert meta["publisher"] == "Zenodo"


def test_datacite_text_is_article_not_record() -> None:
    """A DataCite ``Text``/``JournalArticle`` keeps ``entry_type='article'``
    and gains no ``record_type`` — ordinary papers stay on the passage
    rules."""
    meta = doi_registry._normalize_datacite(
        _datacite_attrs(
            types={"resourceTypeGeneral": "Text"},
            container={"title": "Some Series"},
        ),
        "10.4230/lipics.x.1",
    )
    assert "record_type" not in meta
    assert meta["entry_type"] == "article"
    assert meta["journal"] == "Some Series"
    assert meta["publisher"] == "Zenodo"


def test_datacite_other_type_omits_entry_type() -> None:
    """A non-article, non-record resourceTypeGeneral leaves ``entry_type``
    out entirely — readers default as before, and the publisher no longer
    leaks into ``journal``."""
    meta = doi_registry._normalize_datacite(
        _datacite_attrs(types={"resourceTypeGeneral": "Model"}),
        "10.5281/zenodo.22863181",
    )
    assert "record_type" not in meta
    assert "entry_type" not in meta
    assert meta["journal"] == ""


def test_crossref_dataset_and_software_types_type_the_record() -> None:
    from precis.ingest import crossref

    for raw, rt in (("dataset", "dataset"), ("software", "software")):
        meta = crossref._normalize(
            {"type": raw, "title": ["T"], "DOI": "10.1/x"}, "10.1/x"
        )
        assert meta["record_type"] == rt
        assert meta["entry_type"] == raw
    meta = crossref._normalize(
        {"type": "journal-article", "title": ["T"], "DOI": "10.1/x"}, "10.1/x"
    )
    assert "record_type" not in meta
