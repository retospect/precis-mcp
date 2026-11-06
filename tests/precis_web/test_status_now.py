"""Real-PG regression tests for the Status page's Now sub-tab helpers.

Same rationale as ``test_status_sql.py``: these queries JOIN
``ref_tags``/``tags`` and read jsonb ``meta`` shapes the web ``FakeStore``
doesn't parse, so they run against the live ``store`` fixture directly.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis_web.routes.status as status_module
from precis.alerts import raise_alert, resolve_stale_alerts
from precis.store import Tag
from precis.workers.executors._common import append_chunk
from precis_web.routes.status import _now_alerts, _now_hosts, _now_jobs


def _set_status(store: Any, ref_id: int, value: str) -> None:
    store.add_tag(
        ref_id,
        Tag.parse_strict(f"STATUS:{value}"),
        set_by="agent",
        replace_prefix=True,
    )


def test_now_hosts_reads_running_and_idle_activity_from_heartbeat_meta(
    store: Any,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO host_heartbeat (host, ts, load1, meta)
            VALUES ('melchior-now', now(), 0.5, %s::jsonb)
            """,
            (
                '{"activity": {'
                '"precis-worker": {"pass": "fetch_oa", '
                '"since": "2026-08-09T00:00:00+00:00", "detail": "stub 3/10"}, '
                '"precis-worker-agent": {"idle": true, "last_pass": "chase", '
                '"finished": "2026-08-09T00:05:00+00:00"}'
                "}}",
            ),
        )
        conn.commit()

    hosts = {h["host"]: h for h in _now_hosts(store)}
    row = hosts["melchior-now"]
    by_process = {p["process"]: p for p in row["processes"]}

    running = by_process["precis-worker"]
    assert running["idle"] is False
    assert running["pass_name"] == "fetch_oa"
    assert running["detail"] == "stub 3/10"
    assert running["running_minutes"] > 0

    idle = by_process["precis-worker-agent"]
    assert idle["idle"] is True
    assert idle["last_pass"] == "chase"


def test_now_hosts_handles_no_activity_key(store: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, load1, meta) "
            "VALUES ('melchior-bare', now(), 0.1, '{}'::jsonb)"
        )
        conn.commit()
    hosts = {h["host"]: h for h in _now_hosts(store)}
    assert hosts["melchior-bare"]["processes"] == []


def test_now_jobs_running_queued_stalled_and_terminal(store: Any) -> None:
    running = store.insert_ref(
        kind="job",
        slug=None,
        title="running job",
        meta={
            "job_type": "struct_relax",
            "lease_host": "melchior",
            "lease_until": "2099-01-01T00:00:00+00:00",
        },
    )
    _set_status(store, running.id, "running")

    fresh_queued = store.insert_ref(
        kind="job", slug=None, title="fresh queued", meta={"job_type": "fold"}
    )
    _set_status(store, fresh_queued.id, "queued")

    stale_queued = store.insert_ref(
        kind="job", slug=None, title="stale queued", meta={"job_type": "fold"}
    )
    _set_status(store, stale_queued.id, "queued")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '30 minutes' "
            "WHERE ref_id = %s",
            (stale_queued.id,),
        )
        conn.commit()

    # A soft-deleted queued job must never appear.
    deleted_queued = store.insert_ref(
        kind="job", slug=None, title="deleted queued", meta={"job_type": "fold"}
    )
    _set_status(store, deleted_queued.id, "queued")
    store.retire_ref(deleted_queued.id)

    succeeded = store.insert_ref(
        kind="job", slug=None, title="done job", meta={"job_type": "fold"}
    )
    _set_status(store, succeeded.id, "succeeded")

    jobs = _now_jobs(store)

    running_ids = {j["ref_id"] for j in jobs["running"]}
    assert running.id in running_ids
    running_row = next(j for j in jobs["running"] if j["ref_id"] == running.id)
    assert running_row["job_type"] == "struct_relax"
    assert running_row["lease_host"] == "melchior"
    assert running_row["lease_expired"] is False
    assert running_row["lease_remaining_s"] is not None
    assert running_row["lease_remaining_s"] > 0
    assert running_row["lease_relative"].startswith("in ")
    assert running_row["lease_until_title"]

    queued_ids = {j["ref_id"] for j in jobs["queued"]}
    assert fresh_queued.id in queued_ids
    assert stale_queued.id in queued_ids
    assert deleted_queued.id not in queued_ids

    by_id = {j["ref_id"]: j for j in jobs["queued"]}
    assert by_id[fresh_queued.id]["stalled"] is False
    assert by_id[stale_queued.id]["stalled"] is True

    terminal_ids = {j["ref_id"] for j in jobs["terminal"]}
    assert succeeded.id in terminal_ids
    by_terminal_id = {j["ref_id"]: j for j in jobs["terminal"]}
    assert by_terminal_id[succeeded.id]["status"] == "succeeded"

    assert jobs["running_count"] == len(jobs["running"])
    assert jobs["queued_count"] == len(jobs["queued"])
    assert jobs["stalled_count"] == 1


def test_now_jobs_lease_expired_flag(store: Any) -> None:
    expired = store.insert_ref(
        kind="job",
        slug=None,
        title="expired lease",
        meta={"lease_host": "melchior", "lease_until": "2000-01-01T00:00:00+00:00"},
    )
    _set_status(store, expired.id, "running")

    jobs = _now_jobs(store)
    row = next(j for j in jobs["running"] if j["ref_id"] == expired.id)
    assert row["lease_expired"] is True
    assert row["lease_remaining_s"] is not None
    assert row["lease_remaining_s"] < 0
    assert row["lease_relative"].endswith(" ago")


def test_now_jobs_quest_tick_row_renders_sentence(store: Any) -> None:
    """A live quest_tick row reads as a sentence naming its quest —
    never the stored title's "(unlinked)" (td573331)."""
    quest = store.insert_ref(kind="quest", slug=None, title="Hunt catalysts", meta={})
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="quest_tick (unlinked)",
        meta={
            "job_type": "quest_tick",
            "params": {"quest_id": quest.id},
            "coordinator_state": {"slice_count": 7},
        },
    )
    append_chunk(
        store,
        job.id,
        "job_event",
        "tick #7: ok — 2 candidate(s), 1 sim(s), 3 harvested, "
        "0 graduated, 1 search(es) (+4 papers); frontier moved",
    )
    _set_status(store, job.id, "running")

    jobs = _now_jobs(store)
    row = next(j for j in jobs["running"] if j["ref_id"] == job.id)
    assert "(unlinked)" not in row["sentence"]
    assert "Hunt catalysts" in row["sentence"]
    assert f"qu{quest.id}" in row["sentence"]
    assert "tick 7" in row["sentence"]
    assert row["quest"]["handle"] == f"qu{quest.id}"


def test_now_jobs_struct_quest_tags_one_bulk_query(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page of struct_relax rows resolves its ``quest:<id>`` tags in ONE
    bulk query on the request connection — never a ``tags_for`` per row
    (td573331 fix round)."""
    quest = store.insert_ref(kind="quest", slug=None, title="Bulk quest", meta={})
    for i in range(4):
        struct = store.insert_ref(kind="structure", slug=f"s{i}", title=f"s{i}")
        store.add_tag(struct.id, Tag.open(f"quest:{quest.id}"), set_by="agent")
        job = store.insert_ref(
            kind="job",
            slug=None,
            title=f"relax {i}",
            meta={
                "job_type": "struct_relax",
                "params": {"structure_ref_id": struct.id},
            },
        )
        _set_status(store, job.id, "running")

    calls = {"bulk": 0}
    real = status_module.quest_ids_for_structures

    def spy(conn: Any, structure_ids: Any) -> dict[int, int]:
        calls["bulk"] += 1
        return real(conn, structure_ids)

    monkeypatch.setattr(status_module, "quest_ids_for_structures", spy)
    monkeypatch.setattr(
        type(store),
        "tags_for",
        lambda *a, **k: pytest.fail("per-row tags_for called"),
    )

    jobs = _now_jobs(store)
    assert calls["bulk"] == 1
    rows = [j for j in jobs["running"] if j["job_type"] == "struct_relax"]
    assert len(rows) == 4
    for j in rows:
        assert j["quest"]["handle"] == f"qu{quest.id}"
        assert "Bulk quest" in j["sentence"]


def test_now_jobs_row_without_job_type_keeps_title(store: Any) -> None:
    """A row whose meta names no ``job_type`` gets no sentence — the
    template falls back to the stored title; ``'job: running'`` would be
    worse (td573331 fix round)."""
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="mystery worker",
        meta={"lease_host": "melchior"},
    )
    _set_status(store, job.id, "running")
    row = next(j for j in _now_jobs(store)["running"] if j["ref_id"] == job.id)
    assert row["sentence"] is None
    assert row["title"] == "mystery worker"


def test_now_jobs_upkeep_collapses_into_one_line(store: Any) -> None:
    """Three queued embed_batch jobs render as one platform-upkeep line
    carrying the count — not three queue rows (td573331)."""
    ids = []
    for _ in range(3):
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="embed_batch (materializer 1/3)",
            meta={"job_type": "embed_batch"},
        )
        _set_status(store, ref.id, "queued")
        ids.append(ref.id)

    jobs = _now_jobs(store)
    assert not any(j["job_type"] == "embed_batch" for j in jobs["queued"])
    lines = [u for u in jobs["upkeep"] if u["job_type"] == "embed_batch"]
    assert len(lines) == 1
    line = lines[0]
    assert line["queued"] == 3
    assert "platform upkeep" in line["sentence"]
    assert "embedding new text chunks" in line["sentence"]
    assert "3 queued" in line["sentence"]
    assert line["ref_id"] in ids
    assert jobs["queued_count"] == 3


def test_now_alerts_active_first_then_resolved(store: Any) -> None:
    ref_id, _ = raise_alert(
        store,
        source="test:now_tab",
        fingerprint="now-tab-alert-1",
        title="disk almost full",
        severity="warn",
    )
    resolved_ref_id, _ = raise_alert(
        store,
        source="test:now_tab",
        fingerprint="now-tab-alert-2",
        title="transient blip",
        severity="info",
    )
    resolve_stale_alerts(
        store, source="test:now_tab", live_fingerprints=["now-tab-alert-1"]
    )

    alerts = _now_alerts(store)
    active_ids = {a["ref_id"] for a in alerts["active"]}
    recent_ids = {a["ref_id"] for a in alerts["recent"]}

    assert ref_id in active_ids
    assert resolved_ref_id in recent_ids
    assert resolved_ref_id not in active_ids
