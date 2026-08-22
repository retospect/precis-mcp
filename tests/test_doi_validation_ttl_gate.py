"""The TTL gate in ``precis.ingest.provenance.check_ref_doi_validity`` —
the DOI-completeness check's validity twin of ``check_ref_retraction``
(docs/backlog/draft-doi-completeness-check.md), mirroring
``tests/test_retraction_ttl_gate.py`` structurally.

Unlike retraction, there is no second source (no Retraction-Watch
equivalent) whose knowledge a clean read could clobber — a resolves/
doesn't-resolve answer from Crossref is the whole picture — so a checked
outcome always stamps both ``doi_status`` and ``doi_validated_at``
together (see ``Store.set_doi_validation``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.ingest import provenance as P


class FakeStore:
    def __init__(self, ref, doi=None):
        self._ref = ref
        self._doi = doi
        self.validations: list[dict] = []

    def fetch_refs_by_ids(self, ids, include_deleted=False):
        return {self._ref.id: self._ref} if self._ref is not None else {}

    def dois_for_refs(self, ids):
        return {i: self._doi for i in ids} if self._doi else {}

    def set_doi_validation(self, ref_id, *, status, conn=None):
        self.validations.append({"ref_id": ref_id, "status": status})


class FakeRef:
    def __init__(self, ref_id=1, doi_status=None, doi_validated_at=None):
        self.id = ref_id
        self.kind = "paper"
        self.doi_status = doi_status
        self.doi_validated_at = doi_validated_at


def test_inside_ttl_short_circuits_without_touching_the_network(monkeypatch):
    monkeypatch.setattr(
        P,
        "_fetch_doi_validity",
        lambda *a, **kw: pytest.fail("TTL should have short-circuited"),
    )
    recent = datetime.now(UTC) - timedelta(days=3)
    store: Any = FakeStore(
        FakeRef(doi_status="valid", doi_validated_at=recent), doi="10.1/x"
    )

    out = P.check_ref_doi_validity(store, 1)

    assert out.outcome == "fresh"
    assert out.status == "valid"
    assert out.never_validated is False
    assert store.validations == []


def test_force_ignores_the_ttl(monkeypatch):
    """Without this, mashing the watch button twice is a silent no-op."""
    calls: list[str] = []

    def _checked(doi, *, mailto):
        calls.append(doi)
        return "valid"

    monkeypatch.setattr(P, "_fetch_doi_validity", _checked)
    recent = datetime.now(UTC) - timedelta(days=3)
    store: Any = FakeStore(
        FakeRef(doi_status="valid", doi_validated_at=recent), doi="10.1/x"
    )

    out = P.check_ref_doi_validity(store, 1, force=True)

    assert calls == ["10.1/x"]
    assert out.outcome == "checked"
    assert store.validations == [{"ref_id": 1, "status": "valid"}]


def test_expired_ttl_rechecks(monkeypatch):
    monkeypatch.setattr(P, "_fetch_doi_validity", lambda *a, **kw: "valid")
    stale = datetime.now(UTC) - timedelta(days=P.DOI_VALIDATION_TTL_DAYS + 1)
    store: Any = FakeStore(FakeRef(doi_validated_at=stale), doi="10.1/x")

    assert P.check_ref_doi_validity(store, 1).outcome == "checked"


def test_never_validated_is_checked_and_stamped_valid(monkeypatch):
    monkeypatch.setattr(P, "_fetch_doi_validity", lambda *a, **kw: "valid")
    store: Any = FakeStore(FakeRef(doi_validated_at=None), doi="10.1/x")

    out = P.check_ref_doi_validity(store, 1)

    assert out.outcome == "checked"
    assert out.status == "valid"
    assert out.never_validated is False  # just validated, right now
    assert store.validations == [{"ref_id": 1, "status": "valid"}]


def test_a_dead_doi_is_stamped_not_found(monkeypatch):
    """Unlike retraction's clean-read no-clobber rule, a resolves/doesn't
    answer is always written — there is nothing else to preserve."""
    monkeypatch.setattr(P, "_fetch_doi_validity", lambda *a, **kw: "not_found")
    store: Any = FakeStore(FakeRef(doi_validated_at=None), doi="10.1/dead")

    out = P.check_ref_doi_validity(store, 1)

    assert out.outcome == "checked"
    assert out.status == "not_found"
    assert store.validations == [{"ref_id": 1, "status": "not_found"}]


def test_a_ref_with_no_doi_is_not_stamped(monkeypatch):
    """Stamping here would poison the TTL — we did not check anything."""
    monkeypatch.setattr(
        P, "_fetch_doi_validity", lambda *a, **kw: pytest.fail("no DOI to check")
    )
    store: Any = FakeStore(FakeRef(doi_validated_at=None), doi=None)

    out = P.check_ref_doi_validity(store, 1)

    assert out.outcome == "no_doi"
    assert out.never_validated is True
    assert store.validations == []


def test_upstream_failure_is_not_recorded(monkeypatch):
    monkeypatch.setattr(P, "_fetch_doi_validity", lambda *a, **kw: None)
    store: Any = FakeStore(FakeRef(doi_validated_at=None), doi="10.1/x")

    out = P.check_ref_doi_validity(store, 1)

    assert out.outcome == "unchecked"
    assert out.error is not None
    assert store.validations == []


def test_missing_ref(monkeypatch):
    store: Any = FakeStore(None)
    assert P.check_ref_doi_validity(store, 99).outcome == "missing"


def test_batch_dedupes_and_preserves_order(monkeypatch):
    seen: list[int] = []

    def _one(store, ref_id, **kw):
        seen.append(ref_id)
        return P.DoiValidationCheck(ref_id=ref_id, outcome="checked")

    monkeypatch.setattr(P, "check_ref_doi_validity", _one)

    store: Any = FakeStore(None)
    out = P.check_refs_doi_validity(store, [3, 1, 3, 2, 1])

    assert seen == [3, 1, 2]
    assert [c.ref_id for c in out] == [3, 1, 2]
