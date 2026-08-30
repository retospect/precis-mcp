"""Draft-level retraction walk (``precis.export.retraction``) — and its
DOI-completeness/validity twin (docs/backlog/draft-doi-completeness-check.md).

The web routes mock this module out, so these tests are the only thing
standing between a typo here and a gate that is silently inert in
production — which is exactly how the ``Ref.id`` / ``.ref_id`` slip that
prompted them got as far as it did. They run against real
:class:`precis.store.types.Ref` instances for that reason: a fake with
duck-typed attributes would have happily accepted the wrong one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.export import retraction as R
from precis.ingest import provenance as P
from precis.store.types import Ref


def _ref(
    ref_id: int,
    slug: str,
    *,
    status: str | None = None,
    checked_at: datetime | None = None,
    doi_status: str | None = None,
    doi_validated_at: datetime | None = None,
) -> Ref:
    now = datetime.now(UTC)
    return Ref(
        id=ref_id,
        kind="paper",
        slug=slug,
        title=f"Paper {slug}",
        provider=None,
        meta={},
        created_at=now,
        updated_at=now,
        retired_at=None,
        retraction_status=status,
        retraction_checked_at=checked_at,
        doi_status=doi_status,
        doi_validated_at=doi_validated_at,
    )


class _FakeStore:
    """Just enough of ``Store`` for ``draft_retraction_report``'s presence
    read (:meth:`identifiers_for_refs`) — a real ``object()`` has no such
    method, so any test whose refs list is non-empty needs this instead."""

    def __init__(self, identifiers: dict[int, dict[str, str]] | None = None):
        self._identifiers = identifiers or {}

    def identifiers_for_refs(self, ref_ids):
        return {rid: self._identifiers.get(rid, {}) for rid in ref_ids}


@pytest.fixture
def patched_walk(monkeypatch):
    """Drive ``cited_paper_refs`` off a caller-supplied ref list."""

    def _install(refs, unresolved=()):
        monkeypatch.setattr(
            R,
            "cited_paper_refs",
            lambda store, ref, cited_slugs=None: (list(refs), list(unresolved)),
        )

    return _install


def test_read_mode_uses_stored_status_and_never_checks(patched_walk, monkeypatch):
    """``check=False`` is the export path: stored state only, no network."""
    called = False

    def _boom(*a, **kw):
        nonlocal called
        called = True
        raise AssertionError("export path must never call the checker")

    monkeypatch.setattr(R, "check_refs_retraction", _boom)
    monkeypatch.setattr(R, "check_refs_doi_validity", _boom)
    patched_walk([_ref(1, "smith2024", status="retracted")])

    report = R.draft_retraction_report(_FakeStore(), object())

    assert called is False
    assert report.checked is False
    assert [p.ref_id for p in report.papers] == [1]
    assert report.blocks_export is True
    assert [p.slug for p in report.retracted] == ["smith2024"]


def test_reads_the_ref_id_off_the_real_dataclass(patched_walk, monkeypatch):
    """Regression: ``Ref`` exposes ``.id``, not ``.ref_id``.

    Reading the wrong attribute raised ``AttributeError`` for any draft
    citing at least one resolvable paper — in *both* modes, since the
    loop that built the result ran unconditionally.
    """
    monkeypatch.setattr(
        R,
        "check_refs_retraction",
        lambda store, ref_ids, **kw: [
            _FakeCheck(ref_id=rid, outcome="checked", status=None) for rid in ref_ids
        ],
    )
    monkeypatch.setattr(R, "check_refs_doi_validity", lambda *a, **kw: [])
    patched_walk([_ref(7, "jones2023"), _ref(8, "lee2022")])

    for check in (False, True):
        report = R.draft_retraction_report(_FakeStore(), object(), check=check)
        assert [p.ref_id for p in report.papers] == [7, 8]


class _FakeCheck:
    def __init__(self, ref_id, outcome, status=None, checked_at=None):
        self.ref_id = ref_id
        self.outcome = outcome
        self.status = status
        self.checked_at = checked_at


class _FakeDoiCheck:
    def __init__(self, ref_id, outcome, status=None, validated_at=None):
        self.ref_id = ref_id
        self.outcome = outcome
        self.status = status
        self.validated_at = validated_at


def test_never_checked_is_distinct_from_clean(patched_walk):
    """The whole point of the sparse model: NULL != clean."""
    fresh = datetime.now(UTC) - timedelta(days=1)
    patched_walk(
        [
            _ref(1, "checked-clean", checked_at=fresh),
            _ref(2, "never-looked", checked_at=None),
        ]
    )

    report = R.draft_retraction_report(_FakeStore(), object())

    assert [p.slug for p in report.unchecked] == ["never-looked"]
    assert report.blocks_export is False
    assert "1 never checked" in report.summary()


def test_soft_statuses_annotate_but_do_not_block(patched_walk):
    patched_walk(
        [
            _ref(1, "corrigendum", status="corrected", checked_at=datetime.now(UTC)),
            _ref(
                2,
                "concern",
                status="expression_of_concern",
                checked_at=datetime.now(UTC),
            ),
        ]
    )

    report = R.draft_retraction_report(_FakeStore(), object())

    assert report.blocks_export is False
    assert len(report.soft) == 2
    assert report.papers[0].label == "corrected"
    assert report.papers[1].label == "expression of concern"


def test_check_mode_force_is_passed_through(patched_walk, monkeypatch):
    seen: dict = {}

    def _check(store, ref_ids, **kw):
        seen.update(kw)
        seen["ref_ids"] = list(ref_ids)
        return [_FakeCheck(ref_id=r, outcome="checked", status=None) for r in ref_ids]

    monkeypatch.setattr(R, "check_refs_doi_validity", lambda *a, **kw: [])
    patched_walk([_ref(3, "a"), _ref(4, "b")])

    original = R.check_refs_retraction
    R.check_refs_retraction = _check
    try:
        report = R.draft_retraction_report(
            _FakeStore(), object(), check=True, force=True
        )
    finally:
        R.check_refs_retraction = original

    assert seen["force"] is True
    assert seen["ref_ids"] == [3, 4]
    assert report.checked is True


def test_select_for_check_puts_the_neediest_first(patched_walk):
    """Never-checked leads, then oldest stamp — the property that makes the
    button's cap a per-press budget instead of a horizon. A head slice would
    return ``[fresh, old, never]`` here and strand ``never`` forever."""
    now = datetime.now(UTC)
    fresh = _ref(1, "fresh", checked_at=now - timedelta(days=1))
    old = _ref(2, "old", checked_at=now - timedelta(days=200))
    never = _ref(3, "never")

    picked = R.select_for_check([fresh, old, never], 2)

    assert [r.slug for r in picked] == ["never", "old"]


def test_select_for_check_returns_everything_under_the_cap(patched_walk):
    """Under the cap there is nothing to prioritise — order is left alone so
    a small draft's report reads in cited order."""
    refs = [_ref(1, "a"), _ref(2, "b")]

    assert [r.slug for r in R.select_for_check(refs, 40)] == ["a", "b"]
    assert [r.slug for r in R.select_for_check(refs, 0)] == ["a", "b"]


def test_check_slugs_narrows_the_walk_but_not_the_report(patched_walk, monkeypatch):
    """The subset bounds the *network* walk only. Reporting just the checked
    subset is what made a half-walked draft look complete: the pane's
    "N of M never checked" prompt reads off these totals."""
    seen: dict = {}

    def _check(store, ref_ids, **kw):
        seen["ref_ids"] = list(ref_ids)
        return [
            _FakeCheck(
                ref_id=r, outcome="checked", status=None, checked_at=datetime.now(UTC)
            )
            for r in ref_ids
        ]

    monkeypatch.setattr(R, "check_refs_doi_validity", lambda *a, **kw: [])
    patched_walk([_ref(1, "a"), _ref(2, "b"), _ref(3, "c")])

    original = R.check_refs_retraction
    R.check_refs_retraction = _check
    try:
        report = R.draft_retraction_report(
            _FakeStore(), object(), check=True, check_slugs=["b"]
        )
    finally:
        R.check_refs_retraction = original

    assert seen["ref_ids"] == [2]  # only the selected cite hit the network
    assert [p.slug for p in report.papers] == ["a", "b", "c"]  # report is whole
    # The two that weren't walked keep their stored state — here, unchecked.
    assert [p.slug for p in report.unchecked] == ["a", "c"]


def test_unresolved_slugs_are_reported_not_dropped(patched_walk):
    patched_walk([_ref(1, "real")], unresolved=["ghost2020"])

    report = R.draft_retraction_report(_FakeStore(), object())

    assert report.unresolved == ["ghost2020"]
    assert len(report.papers) == 1


# ---------------------------------------------------------------------------
# DOI completeness (presence) — docs/backlog/draft-doi-completeness-check.md
# ---------------------------------------------------------------------------


def test_doi_presence_three_bucket_partition(patched_walk):
    """A cite with a DOI, one with only an arxiv id, and a bare stub with
    neither partition into clean / fetchable / no-persistent-identifier —
    the acceptance criterion's three-way split."""
    patched_walk(
        [
            _ref(1, "has-doi"),
            _ref(2, "arxiv-only"),
            _ref(3, "bare-stub"),
        ]
    )
    store = _FakeStore(
        {
            1: {"doi": "10.1/x"},
            2: {"arxiv": "1234.5678"},
            3: {},
        }
    )

    report = R.draft_retraction_report(store, object())
    by_slug = {p.slug: p for p in report.papers}

    assert by_slug["has-doi"].has_doi is True
    assert by_slug["has-doi"].doi_presence_label == ""

    assert by_slug["arxiv-only"].has_doi is False
    assert by_slug["arxiv-only"].doi_fetchable is True
    assert by_slug["arxiv-only"].doi_presence_label == "no DOI (fetchable from arxiv)"

    assert by_slug["bare-stub"].has_doi is False
    assert by_slug["bare-stub"].doi_missing_no_identifier is True
    assert by_slug["bare-stub"].doi_presence_label == "no persistent identifier"

    assert [p.slug for p in report.missing_doi] == ["arxiv-only", "bare-stub"]
    assert [p.slug for p in report.doi_fetchable] == ["arxiv-only"]
    assert [p.slug for p in report.doi_no_identifier] == ["bare-stub"]
    assert "2 missing DOI" in report.summary()


def test_doi_presence_is_a_pure_read_no_network(patched_walk, monkeypatch):
    """Presence never touches the network, even on the check=True path —
    only validity does."""
    monkeypatch.setattr(R, "check_refs_retraction", lambda *a, **kw: [])
    monkeypatch.setattr(R, "check_refs_doi_validity", lambda *a, **kw: [])
    patched_walk([_ref(1, "arxiv-only")])
    store = _FakeStore({1: {"arxiv": "1234.5678"}})

    report = R.draft_retraction_report(store, object())

    assert report.papers[0].doi_fetchable is True
    assert report.blocks_export is False


def test_doi_presence_never_blocks_export(patched_walk):
    patched_walk([_ref(1, "bare-stub")])
    store = _FakeStore({1: {}})

    report = R.draft_retraction_report(store, object())

    assert report.doi_no_identifier
    assert report.blocks_export is False


# ---------------------------------------------------------------------------
# DOI validity — network check, TTL-gated, riding the retraction-watch button
# ---------------------------------------------------------------------------


def test_never_validated_doi_is_its_own_state(patched_walk):
    """A ref with a DOI that has never been asked about reads as
    ``doi_never_validated`` — not rounded down to "valid"."""
    patched_walk([_ref(1, "has-doi")])
    store = _FakeStore({1: {"doi": "10.1/x"}})

    report = R.draft_retraction_report(store, object())

    assert report.papers[0].doi_never_validated is True
    assert [p.slug for p in report.doi_unvalidated] == ["has-doi"]
    assert "1 DOI never validated" in report.summary()


def test_validated_doi_shows_the_stamp(patched_walk):
    when = datetime.now(UTC) - timedelta(days=2)
    patched_walk([_ref(1, "has-doi", doi_status="valid", doi_validated_at=when)])
    store = _FakeStore({1: {"doi": "10.1/x"}})

    report = R.draft_retraction_report(store, object())

    p = report.papers[0]
    assert p.doi_never_validated is False
    assert p.doi_validated_at == when
    assert p.doi_invalid is False


def test_watch_button_press_validates_dois_in_the_same_pass(patched_walk, monkeypatch):
    """Pressing the retraction-watch button (``check=True``) also validates
    DOIs off the same ``to_check`` subset — one press, both signals."""
    seen: dict = {}

    def _doi_check(store, ref_ids, **kw):
        seen["doi_ref_ids"] = list(ref_ids)
        seen["doi_kw"] = kw
        return [
            _FakeDoiCheck(
                ref_id=1,
                outcome="checked",
                status="valid",
                validated_at=datetime.now(UTC),
            ),
            _FakeDoiCheck(
                ref_id=2,
                outcome="checked",
                status="not_found",
                validated_at=datetime.now(UTC),
            ),
        ]

    monkeypatch.setattr(R, "check_refs_retraction", lambda *a, **kw: [])
    monkeypatch.setattr(R, "check_refs_doi_validity", _doi_check)
    patched_walk(
        [
            _ref(1, "resolving", doi_validated_at=None),
            _ref(2, "dead-doi", doi_validated_at=None),
        ]
    )
    store = _FakeStore({1: {"doi": "10.1/live"}, 2: {"doi": "10.1/dead"}})

    report = R.draft_retraction_report(
        store, object(), check=True, force=True, mailto="a@b.c"
    )

    assert seen["doi_ref_ids"] == [1, 2]
    assert seen["doi_kw"]["force"] is True
    assert seen["doi_kw"]["mailto"] == "a@b.c"
    by_slug = {p.slug: p for p in report.papers}
    assert by_slug["resolving"].doi_status == "valid"
    assert by_slug["resolving"].doi_never_validated is False
    assert by_slug["dead-doi"].doi_invalid is True
    assert [p.slug for p in report.doi_invalid] == ["dead-doi"]
    assert report.blocks_export is False


def test_doi_invalid_never_blocks_export(patched_walk, monkeypatch):
    monkeypatch.setattr(R, "check_refs_retraction", lambda *a, **kw: [])
    monkeypatch.setattr(
        R,
        "check_refs_doi_validity",
        lambda store, ref_ids, **kw: [
            _FakeDoiCheck(ref_id=r, outcome="checked", status="not_found")
            for r in ref_ids
        ],
    )
    patched_walk([_ref(1, "dead-doi")])
    store = _FakeStore({1: {"doi": "10.1/dead"}})

    report = R.draft_retraction_report(store, object(), check=True)

    assert report.doi_invalid
    assert report.blocks_export is False


def test_all_validated_dois_read_as_all_clear(patched_walk):
    """A draft whose cites all carry a validated DOI shows an all-clear
    summary — the DOI signals don't leak a false "missing"/"never
    validated" bit once everything has actually been checked."""
    when = datetime.now(UTC)
    patched_walk(
        [
            _ref(1, "a", checked_at=when, doi_status="valid", doi_validated_at=when),
            _ref(2, "b", checked_at=when, doi_status="valid", doi_validated_at=when),
        ]
    )
    store = _FakeStore({1: {"doi": "10.1/a"}, 2: {"doi": "10.1/b"}})

    report = R.draft_retraction_report(store, object())

    assert not report.missing_doi
    assert not report.doi_unvalidated
    assert not report.doi_invalid
    assert report.summary() == "2 cited papers, all clean"


# ---------------------------------------------------------------------------
# Watch-button single-fetch guarantee (pre-ship-review fix): the retraction
# check and DOI-validity check used to each GET
# api.crossref.org/works/{doi} for the same cite. These drive the *real*
# check_refs_retraction/check_refs_doi_validity (only precis.ingest.
# provenance.check_doi and ._fetch_doi_validity are mocked), so a
# regression that reintroduces the second sweep shows up here even though
# the other tests in this module mock the two check_refs_* functions out.
# ---------------------------------------------------------------------------


class _ButtonStore:
    """Enough ``Store`` surface for a real ``check_refs_retraction`` +
    ``check_refs_doi_validity`` walk: no habanero/httpx, just the DB-shaped
    calls those functions make."""

    def __init__(self, refs_by_id: dict[int, Ref], dois: dict[int, str]):
        self._refs = refs_by_id
        self._dois = dois
        self.doi_writes: list[tuple[int, str]] = []

    def fetch_refs_by_ids(self, ids, include_deleted=False):
        return {i: self._refs[i] for i in ids if i in self._refs}

    def dois_for_refs(self, ids):
        return {i: self._dois[i] for i in ids if self._dois.get(i)}

    def touch_retraction_checked(self, ref_id, conn=None):
        pass

    def set_retraction_status(self, ref_id, **kw):
        return 0

    def set_doi_validation(self, ref_id, *, status, conn=None):
        self.doi_writes.append((ref_id, status))

    def identifiers_for_refs(self, ref_ids):
        return {
            rid: ({"doi": self._dois[rid]} if self._dois.get(rid) else {})
            for rid in ref_ids
        }


def _boom_fetch_doi_validity(*a, **kw):
    pytest.fail(
        "check_refs_doi_validity must not re-fetch a cite the retraction "
        "check already resolved this press — that is the doubled-fetch bug"
    )


def test_watch_button_one_crossref_fetch_per_cold_resolving_cite(
    patched_walk, monkeypatch
):
    calls: list[str] = []

    def _check_doi(doi, *, store=None, mailto=None, **kw):
        calls.append(doi)
        return P.ProvenanceResult(doi=doi, status="ok", applied_status=None)

    monkeypatch.setattr(P, "check_doi", _check_doi)
    monkeypatch.setattr(P, "_fetch_doi_validity", _boom_fetch_doi_validity)

    ref = _ref(1, "cold-cite")
    patched_walk([ref])
    store = _ButtonStore({1: ref}, {1: "10.1/cold"})

    report = R.draft_retraction_report(store, object(), check=True, mailto="a@b.c")

    assert calls == ["10.1/cold"]  # exactly one Crossref round-trip, not two
    assert store.doi_writes == [(1, "valid")]
    p = report.papers[0]
    assert p.doi_status == "valid"
    assert p.doi_never_validated is False
    assert p.status is None
    assert report.blocks_export is False


def test_watch_button_stamps_not_found_from_a_definitive_404(patched_walk, monkeypatch):
    """A Crossref 404 with nothing in the RW cache is ``check_doi``'s
    ``status='unknown'`` — the one case safe to stamp ``not_found`` off
    the retraction fetch alone."""
    monkeypatch.setattr(
        P, "check_doi", lambda doi, **kw: P.ProvenanceResult(doi=doi, status="unknown")
    )
    monkeypatch.setattr(P, "_fetch_doi_validity", _boom_fetch_doi_validity)

    ref = _ref(1, "dead-doi")
    patched_walk([ref])
    store = _ButtonStore({1: ref}, {1: "10.1/dead"})

    report = R.draft_retraction_report(store, object(), check=True)

    assert store.doi_writes == [(1, "not_found")]
    assert report.papers[0].doi_invalid is True
    assert report.blocks_export is False


def test_watch_button_does_not_stamp_on_transport_failure(patched_walk, monkeypatch):
    """A network hiccup is not evidence either way — must not be stamped,
    and must not trigger a second fetch attempt through the validity path
    (that cite already spent its one round-trip this press)."""
    monkeypatch.setattr(
        P,
        "check_doi",
        lambda doi, **kw: P.ProvenanceResult(
            doi=doi, status="check_failed", error="boom"
        ),
    )
    monkeypatch.setattr(P, "_fetch_doi_validity", _boom_fetch_doi_validity)

    ref = _ref(1, "flaky")
    patched_walk([ref])
    store = _ButtonStore({1: ref}, {1: "10.1/flaky"})

    report = R.draft_retraction_report(store, object(), check=True)

    assert store.doi_writes == []
    assert report.papers[0].doi_never_validated is True


def test_watch_button_still_validates_a_ttl_fresh_retraction_cite(
    patched_walk, monkeypatch
):
    """When the retraction check itself short-circuits on its own TTL (no
    network spent), DOI validity still gets its one round-trip through
    ``check_refs_doi_validity`` — the fix narrows the skip to cites the
    retraction check actually spent a fetch on, not to the whole press."""
    doi_calls: list[int] = []

    def _fetch_validity(doi, *, mailto=None):
        doi_calls.append(1)
        return "valid"

    monkeypatch.setattr(
        P, "check_doi", lambda *a, **kw: pytest.fail("retraction TTL should skip this")
    )
    monkeypatch.setattr(P, "_fetch_doi_validity", _fetch_validity)

    fresh = datetime.now(UTC) - timedelta(days=1)
    ref = _ref(1, "recently-checked", checked_at=fresh)
    patched_walk([ref])
    store = _ButtonStore({1: ref}, {1: "10.1/recently-checked"})

    report = R.draft_retraction_report(store, object(), check=True)

    assert doi_calls == [1]  # exactly one round-trip, via the validity path
    assert store.doi_writes == [(1, "valid")]
    assert report.papers[0].doi_status == "valid"


# ---------------------------------------------------------------------------
# Shared classification helpers
# ---------------------------------------------------------------------------


def test_identifier_kind_for_prefers_arxiv_then_pubmed_then_s2():
    assert R.identifier_kind_for({"arxiv": "1", "s2": "2"}) == "arxiv"
    assert R.identifier_kind_for({"pubmed": "1", "s2": "2"}) == "pubmed"
    assert R.identifier_kind_for({"s2": "2"}) == "s2"
    assert R.identifier_kind_for({}) is None
    # cite_key / other schemes don't count as "fetchable".
    assert R.identifier_kind_for({"cite_key": "smith24", "openalex": "W1"}) is None


def test_summarize_doi_completeness_all_clear_and_mixed():
    refs_by_id = {
        1: _ref(1, "a", doi_status="valid", doi_validated_at=datetime.now(UTC)),
        2: _ref(2, "b"),
        3: _ref(3, "c"),
    }
    identifiers = {1: {"doi": "10.1/a"}, 2: {"arxiv": "1"}, 3: {}}

    line = R.summarize_doi_completeness([1, 2, 3], identifiers, refs_by_id)
    # ref 1 has a doi; ref 2 is missing but fetchable (arxiv); ref 3 is
    # missing with no identifier at all.
    assert "2 missing DOI (1 fetchable, 1 no identifier)" in line

    clean = R.summarize_doi_completeness([1], identifiers, refs_by_id)
    assert clean == "all 1 cited paper(s) have a validated DOI"

    assert R.summarize_doi_completeness([], {}, {}) == ""
