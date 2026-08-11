"""The TTL gate in ``precis.ingest.provenance.check_ref_retraction``.

Coverage is sparse by design (two triggers, no corpus sweep), which puts
all the weight on this one function behaving exactly right: it is what
makes a repeated check free, and what keeps "never looked" from being
silently rounded down to "clean".

The no-clobber rule is the subtle one and has a real failure mode behind
it: Crossref does not know about Retraction-Watch-only notices, so a
clean Crossref read must move the timestamp without touching the status
columns. Writing ``set_retraction_status(status=None)`` there would
un-retract papers.
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
        self.touched: list[int] = []
        self.status_writes: list[dict] = []

    def fetch_refs_by_ids(self, ids, include_deleted=False):
        return {self._ref.id: self._ref} if self._ref is not None else {}

    def dois_for_refs(self, ids):
        return {i: self._doi for i in ids} if self._doi else {}

    def touch_retraction_checked(self, ref_id, conn=None):
        self.touched.append(ref_id)

    def set_retraction_status(self, ref_id, **kw):
        self.status_writes.append({"ref_id": ref_id, **kw})
        return 0


class FakeRef:
    def __init__(self, ref_id=1, status=None, checked_at=None):
        self.id = ref_id
        self.kind = "paper"
        self.retraction_status = status
        self.retraction_checked_at = checked_at


def _result(status="ok", applied=None, error=None):
    return P.ProvenanceResult(
        doi="10.1/x", status=status, applied_status=applied, error=error
    )


def test_inside_ttl_short_circuits_without_touching_the_network(monkeypatch):
    monkeypatch.setattr(
        P, "check_doi", lambda *a, **kw: pytest.fail("TTL should have short-circuited")
    )
    recent = datetime.now(UTC) - timedelta(days=3)
    store: Any = FakeStore(FakeRef(checked_at=recent), doi="10.1/x")

    out = P.check_ref_retraction(store, 1)

    assert out.outcome == "fresh"
    assert out.known_clean is True
    assert store.touched == []


def test_force_ignores_the_ttl(monkeypatch):
    """Without this, mashing the watch button twice is a silent no-op."""
    calls: list[int] = []

    def _checked(*a: Any, **kw: Any) -> P.ProvenanceResult:
        calls.append(1)
        return _result()

    monkeypatch.setattr(P, "check_doi", _checked)
    recent = datetime.now(UTC) - timedelta(days=3)
    store: Any = FakeStore(FakeRef(checked_at=recent), doi="10.1/x")

    out = P.check_ref_retraction(store, 1, force=True)

    assert calls == [1]
    assert out.outcome == "checked"


def test_expired_ttl_rechecks(monkeypatch):
    monkeypatch.setattr(P, "check_doi", lambda *a, **kw: _result())
    stale = datetime.now(UTC) - timedelta(days=P.RETRACTION_TTL_DAYS + 1)
    store: Any = FakeStore(FakeRef(checked_at=stale), doi="10.1/x")

    assert P.check_ref_retraction(store, 1).outcome == "checked"


def test_clean_read_stamps_the_timestamp_only(monkeypatch):
    """A clean answer must be recorded, or every trigger refetches forever."""
    monkeypatch.setattr(P, "check_doi", lambda *a, **kw: _result(applied=None))
    store: Any = FakeStore(FakeRef(checked_at=None), doi="10.1/x")

    out = P.check_ref_retraction(store, 1)

    assert out.outcome == "checked"
    assert out.known_clean is True
    assert store.touched == [1]
    # Never the status columns — that is the clobber this guards against.
    assert store.status_writes == []


def test_clean_crossref_read_does_not_overturn_an_existing_flag(monkeypatch):
    """Crossref is blind to Retraction-Watch-only notices."""
    monkeypatch.setattr(P, "check_doi", lambda *a, **kw: _result(applied=None))
    store: Any = FakeStore(FakeRef(status="retracted", checked_at=None), doi="10.1/x")

    out = P.check_ref_retraction(store, 1)

    assert out.status == "retracted"
    assert out.flagged is True
    assert out.known_clean is False
    assert store.status_writes == []


def test_a_ref_with_no_doi_is_not_stamped(monkeypatch):
    """Stamping here would poison the TTL — we did not check anything."""
    monkeypatch.setattr(P, "check_doi", lambda *a, **kw: pytest.fail("no DOI to check"))
    store: Any = FakeStore(FakeRef(checked_at=None), doi=None)

    out = P.check_ref_retraction(store, 1)

    assert out.outcome == "no_doi"
    assert out.known_clean is False
    assert store.touched == []


def test_upstream_failure_is_not_recorded_as_clean(monkeypatch):
    monkeypatch.setattr(
        P, "check_doi", lambda *a, **kw: _result(status="check_failed", error="boom")
    )
    store: Any = FakeStore(FakeRef(checked_at=None), doi="10.1/x")

    out = P.check_ref_retraction(store, 1)

    assert out.outcome == "unchecked"
    assert out.known_clean is False
    assert out.error == "boom"
    assert store.touched == []


def test_missing_ref(monkeypatch):
    store: Any = FakeStore(None)
    assert P.check_ref_retraction(store, 99).outcome == "missing"


def test_batch_dedupes_and_preserves_order(monkeypatch):
    seen: list[int] = []

    def _one(store, ref_id, **kw):
        seen.append(ref_id)
        return P.RetractionCheck(ref_id=ref_id, outcome="checked")

    monkeypatch.setattr(P, "check_ref_retraction", _one)

    store: Any = FakeStore(None)
    out = P.check_refs_retraction(store, [3, 1, 3, 2, 1])

    assert seen == [3, 1, 2]
    assert [c.ref_id for c in out] == [3, 1, 2]
