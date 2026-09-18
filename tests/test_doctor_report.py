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


def test_reports_are_filed_under_one_shared_folder(store: Store) -> None:
    ref1, _ = doctor_report.find_or_create_report(store, "2026-08-23")
    ref2, _ = doctor_report.find_or_create_report(store, "2026-08-24")

    folder_ids = store.folder_ref_ids_by_title(doctor_report.FOLDER)
    assert len(folder_ids) == 1, "the folder is created once, then reused"
    children = store.folder_subtree_ids(folder_ids[0])
    assert {int(ref1.id), int(ref2.id)} <= children


def test_report_slug_and_date_tag() -> None:
    assert doctor_report.report_slug("2026-08-23") == "doctor-2026-08-23"
    tag = doctor_report.utc_date_tag(datetime(2026, 8, 23, 5, tzinfo=UTC))
    assert tag == "2026-08-23"


# ── strip_preamble: the body starts at ## Classification ─────────


def test_strip_preamble_drops_the_models_sign_off_chatter() -> None:
    """The two real 2026-09-18 reports (dr346343, dr345619) both opened with
    a line addressed to nobody before the required heading."""
    reply = (
        "All writes are done. Filing the report now.\n\n"
        "## Classification\n- nursery: baseline noise\n\n"
        "## Diagnosis\nnothing to localize\n"
    )

    body = doctor_report.strip_preamble(reply)

    assert body is not None
    assert body.startswith("## Classification")
    assert "Filing the report now" not in body
    assert "## Diagnosis" in body


def test_strip_preamble_leaves_a_clean_report_untouched() -> None:
    reply = "## Classification\nall green\n\n## Diagnosis\nnothing\n"

    assert doctor_report.strip_preamble(reply) == reply.strip()


def test_strip_preamble_tolerates_heading_level_and_case() -> None:
    body = doctor_report.strip_preamble("chatter\n\n### CLASSIFICATION\nall green\n")

    assert body == "### CLASSIFICATION\nall green"


def test_strip_preamble_ignores_a_mid_sentence_mention() -> None:
    """Line-anchored: the heading has to open a line, so prose that merely
    names the section is still a preamble — and a reply that is *only* that
    prose has no report in it."""
    assert (
        doctor_report.strip_preamble("I will now write the ## Classification") is None
    )


def test_strip_preamble_without_the_heading_is_none() -> None:
    assert doctor_report.strip_preamble("All done, nothing to report today.") is None
    assert doctor_report.strip_preamble("") is None


# ── convert_needs_a_human: bullets become waiting-for:reto todos ─────


def _body_with_asks(*bullets: str) -> str:
    lines = "\n".join(f"- {b}" for b in bullets)
    return (
        f"## Classification\n- nursery: baseline noise\n\n## Needs a human\n{lines}\n"
    )


def test_convert_needs_a_human_mints_one_todo_per_bullet(store: Store) -> None:
    body = _body_with_asks(
        "Confirm the nursery fix landed (gr111)",
        "Decide whether to raise PRECIS_BACKLOG_GROOM_REFRESH_HOURS",
        "Look at worker_logs for host melchior",
    )

    new_body = doctor_report.convert_needs_a_human(store, body)

    todos = store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20)
    assert len(todos) == 3
    root_id = doctor_report._ensure_asks_root(store)
    for todo in todos:
        assert int(todo.parent_id or -1) == root_id
        assert store.has_tag(int(todo.id), "OPEN", "waiting-for:reto")
        assert todo.meta.get("seen_count") == 1
        assert todo.meta.get("doctor_ask_key")
    ids = sorted(int(t.id) for t in todos)
    for todo_id in ids:
        assert f"- td{todo_id}:" in new_body
    assert "## Needs a human" in new_body


def test_convert_needs_a_human_nested_sub_bullet_joins_the_parent(store: Store) -> None:
    """A sub-bullet indented under an ask is a continuation, not a new
    top-level item — only a column-0 marker starts a fresh ask."""
    body = (
        "## Classification\n- nursery: baseline noise\n\n"
        "## Needs a human\n"
        "- Confirm the retry landed\n"
        "  - saw it fail twice more since\n"
        "- Second ask\n"
    )

    doctor_report.convert_needs_a_human(store, body)

    todos = store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20)
    assert len(todos) == 2
    first = next(t for t in todos if t.title == "Confirm the retry landed")
    chunks = store.chunks.list_chunks_for_ref(int(first.id))
    assert any("saw it fail twice more since" in (c.text or "") for c in chunks)


def test_convert_needs_a_human_dedups_and_bumps_seen_count(store: Store) -> None:
    body = _body_with_asks("Confirm the nursery fix landed (gr111)")

    first_body = doctor_report.convert_needs_a_human(store, body)
    before = store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20)
    assert len(before) == 1

    second_body = doctor_report.convert_needs_a_human(store, body)
    after = store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20)

    assert len(after) == 1, "same bullet re-run must not mint a second todo"
    bumped = store.get_ref(kind="todo", id=int(after[0].id))
    assert bumped is not None
    assert bumped.meta.get("seen_count") == 2
    assert f"td{after[0].id}" in first_body
    assert "seen 2×" in second_body


def test_convert_needs_a_human_dedups_across_a_changed_id_or_number(
    store: Store,
) -> None:
    body_a = _body_with_asks("Still waiting 6 hours on gr111 to be confirmed")
    body_b = _body_with_asks("Still waiting 9 hours on gr222 to be confirmed")

    doctor_report.convert_needs_a_human(store, body_a)
    doctor_report.convert_needs_a_human(store, body_b)

    todos = store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20)
    assert len(todos) == 1
    assert todos[0].meta.get("seen_count") == 2


def test_convert_needs_a_human_no_section_is_a_no_op(store: Store) -> None:
    body = "## Classification\n- nursery: baseline noise\n\n## Diagnosis\nnothing\n"

    new_body = doctor_report.convert_needs_a_human(store, body)

    assert new_body == body
    assert store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20) == []


def test_convert_needs_a_human_malformed_section_leaves_body_untouched(
    store: Store,
) -> None:
    body = (
        "## Classification\n- nursery: baseline noise\n\n"
        "## Needs a human\nJust some prose, no bullets at all.\n"
    )

    new_body = doctor_report.convert_needs_a_human(store, body)

    assert new_body == body
    assert store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20) == []


def test_convert_needs_a_human_carries_the_report_ref_id(store: Store) -> None:
    ref, _ = doctor_report.find_or_create_report(store, "2026-09-18")
    body = _body_with_asks("Confirm the nursery fix landed (gr111)")

    doctor_report.convert_needs_a_human(store, body, report_ref_id=int(ref.id))

    todos = store.list_refs(kind="todo", tags=["waiting-for:reto"], limit=20)
    assert len(todos) == 1
    assert todos[0].meta.get("doctor_report_id") == int(ref.id)
