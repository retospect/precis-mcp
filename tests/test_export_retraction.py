"""Draft-level retraction walk (``precis.export.retraction``).

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
from precis.store.types import Ref


def _ref(
    ref_id: int,
    slug: str,
    *,
    status: str | None = None,
    checked_at: datetime | None = None,
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
        deleted_at=None,
        retraction_status=status,
        retraction_checked_at=checked_at,
    )


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
    patched_walk([_ref(1, "smith2024", status="retracted")])

    report = R.draft_retraction_report(object(), object())

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
    patched_walk([_ref(7, "jones2023"), _ref(8, "lee2022")])

    for check in (False, True):
        report = R.draft_retraction_report(object(), object(), check=check)
        assert [p.ref_id for p in report.papers] == [7, 8]


class _FakeCheck:
    def __init__(self, ref_id, outcome, status=None, checked_at=None):
        self.ref_id = ref_id
        self.outcome = outcome
        self.status = status
        self.checked_at = checked_at


def test_never_checked_is_distinct_from_clean(patched_walk):
    """The whole point of the sparse model: NULL != clean."""
    fresh = datetime.now(UTC) - timedelta(days=1)
    patched_walk(
        [
            _ref(1, "checked-clean", checked_at=fresh),
            _ref(2, "never-looked", checked_at=None),
        ]
    )

    report = R.draft_retraction_report(object(), object())

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

    report = R.draft_retraction_report(object(), object())

    assert report.blocks_export is False
    assert len(report.soft) == 2
    assert report.papers[0].label == "corrected"
    assert report.papers[1].label == "expression of concern"


def test_check_mode_force_is_passed_through(patched_walk):
    seen: dict = {}

    def _check(store, ref_ids, **kw):
        seen.update(kw)
        seen["ref_ids"] = list(ref_ids)
        return [_FakeCheck(ref_id=r, outcome="checked", status=None) for r in ref_ids]

    patched_walk([_ref(3, "a"), _ref(4, "b")])

    original = R.check_refs_retraction
    R.check_refs_retraction = _check
    try:
        report = R.draft_retraction_report(object(), object(), check=True, force=True)
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


def test_check_slugs_narrows_the_walk_but_not_the_report(patched_walk):
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

    patched_walk([_ref(1, "a"), _ref(2, "b"), _ref(3, "c")])

    original = R.check_refs_retraction
    R.check_refs_retraction = _check
    try:
        report = R.draft_retraction_report(
            object(), object(), check=True, check_slugs=["b"]
        )
    finally:
        R.check_refs_retraction = original

    assert seen["ref_ids"] == [2]  # only the selected cite hit the network
    assert [p.slug for p in report.papers] == ["a", "b", "c"]  # report is whole
    # The two that weren't walked keep their stored state — here, unchecked.
    assert [p.slug for p in report.unchecked] == ["a", "c"]


def test_unresolved_slugs_are_reported_not_dropped(patched_walk):
    patched_walk([_ref(1, "real")], unresolved=["ghost2020"])

    report = R.draft_retraction_report(object(), object())

    assert report.unresolved == ["ghost2020"]
    assert len(report.papers) == 1
