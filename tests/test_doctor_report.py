"""``doctor_report`` — the per-UTC-day report artifact contract
(``docs/backlog/doctor-tick-report.md`` item 2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.store import Store
from precis.workers import doctor_report

pytestmark = pytest.mark.db


def test_find_or_create_is_idempotent_per_day(store: Store) -> None:
    ref1, created1 = doctor_report.find_or_create_report(store, "2026-08-23")
    ref2, created2 = doctor_report.find_or_create_report(store, "2026-08-23")

    assert created1 is True
    assert created2 is False
    assert int(ref1.id) == int(ref2.id)
    assert ref1.meta.get("author") == "doctor"


def test_find_or_create_distinct_days_get_distinct_refs(store: Store) -> None:
    ref1, _ = doctor_report.find_or_create_report(store, "2026-08-23")
    ref2, _ = doctor_report.find_or_create_report(store, "2026-08-24")

    assert int(ref1.id) != int(ref2.id)


def test_find_report_absent_returns_none(store: Store) -> None:
    assert doctor_report.find_report(store, "2099-01-01") is None


def test_latest_report_none_when_absent(store: Store) -> None:
    assert doctor_report.latest_report(store) is None


def test_latest_report_reads_body_and_headline(store: Store) -> None:
    date_tag = doctor_report.utc_date_tag()
    ref, _ = doctor_report.find_or_create_report(store, date_tag)
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="## Classification\nall green"
    )

    got = doctor_report.latest_report(store)

    assert got is not None
    assert got.ref_id == int(ref.id)
    assert "all green" in got.body
    assert got.headline  # the draft's title


def test_latest_report_picks_the_most_recent_day(store: Store) -> None:
    doctor_report.find_or_create_report(store, "2020-01-01")
    newer, _ = doctor_report.find_or_create_report(store, "2020-01-02")

    got = doctor_report.latest_report(store)

    assert got is not None
    assert got.ref_id == int(newer.id)


def test_latest_report_max_age_filters_stale(store: Store) -> None:
    ref, _ = doctor_report.find_or_create_report(store, "2020-01-01")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (datetime.now(UTC) - timedelta(hours=48), ref.id),
        )
        conn.commit()

    assert doctor_report.latest_report(store, max_age=timedelta(hours=12)) is None
    assert doctor_report.latest_report(store, max_age=timedelta(hours=72)) is not None


def test_latest_report_fresh_via_recent_append_despite_old_ref(store: Store) -> None:
    """A same-day re-tick appends a paragraph without refreshing the ref's
    ``created_at`` — freshness must follow that append, not the day the
    report was first minted."""
    ref, _ = doctor_report.find_or_create_report(store, "2020-01-01")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (datetime.now(UTC) - timedelta(hours=48), ref.id),
        )
        conn.commit()
    store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="fresh tick")

    got = doctor_report.latest_report(store, max_age=timedelta(hours=12))

    assert got is not None
    assert got.ref_id == int(ref.id)


def test_latest_report_stale_when_no_recent_append(store: Store) -> None:
    """No body chunk at all falls back to the ref's own (old) ``created_at``
    and reads stale — same as before a first tick has appended anything."""
    ref, _ = doctor_report.find_or_create_report(store, "2020-01-01")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (datetime.now(UTC) - timedelta(hours=48), ref.id),
        )
        conn.commit()

    assert doctor_report.latest_report(store, max_age=timedelta(hours=12)) is None


def test_report_slug_and_date_tag() -> None:
    assert doctor_report.report_slug("2026-08-23") == "doctor-2026-08-23"
    tag = doctor_report.utc_date_tag(datetime(2026, 8, 23, 5, tzinfo=UTC))
    assert tag == "2026-08-23"
