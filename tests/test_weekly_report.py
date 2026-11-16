"""Weekly report windows, job output, draft preservation, and Git provenance."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.reports.weekly import collect, git_rows, render_markdown, write_tables
from precis.store import Store, Tag
from precis.store.types import ChunkInsert
from precis.workers.job_types.weekly_report import SPEC, _dispatch

LO = datetime(2026, 11, 1, tzinfo=UTC)
HI = LO + timedelta(days=7)


def _job(
    store: Store, kind: str, when: datetime, state: str = "succeeded", **meta: Any
) -> int:
    ref = store.insert_ref(
        kind="job", slug=None, title="report test", meta={"job_type": kind, **meta}
    )
    store.add_tag(ref.id, Tag.closed("STATUS", state), set_by="system")
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET created_at=%s WHERE ref_id=%s", (when, ref.id))
        conn.execute(
            "UPDATE ref_tags SET created_at=%s WHERE ref_id=%s", (when, ref.id)
        )
        conn.commit()
    return ref.id


def _gripe(store: Store, when: datetime, closed: bool = False) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT public.file_gripe_readonly(%s)", ("weekly test",)
        ).fetchone()
        assert row is not None
        ref_id = int(row[0])
        conn.commit()
    if closed:
        store.add_tag(
            ref_id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
        )
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET created_at=%s WHERE ref_id=%s", (when, ref_id))
        conn.execute(
            "UPDATE ref_tags SET created_at=%s WHERE ref_id=%s", (when, ref_id)
        )
        conn.commit()
    return ref_id


def test_collect_counts(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_WEEKLY_REPORT_CHECKOUT", raising=False)
    _job(store, "struct_relax", LO)
    _job(store, "land_slice", LO, "succeeded")
    _job(store, "land_slice", HI - timedelta(seconds=1), "failed")
    _job(store, "land_slice", HI, "succeeded")
    _job(store, "land_slice", LO - timedelta(seconds=1), "failed")
    _job(store, "release_cut", LO, release_cut={"rN": 42, "sha": "abc"})
    _job(store, "release_confirm", HI, release_confirm={"rN": 43, "sha": "outside"})
    _gripe(store, LO, closed=True)
    _gripe(store, HI, closed=True)
    _gripe(store, LO - timedelta(days=2))
    data = collect(store, LO, HI)
    assert data["Gripes filed and closed"] == [
        {"event": "filed", "count": 1},
        {"event": "closed", "count": 1},
    ]
    assert data["Land slice success"] == [
        {"succeeded": 1, "terminal": 2, "success_percent": 50}
    ]
    assert len(data["Releases"]) == 1
    assert data["Releases"][0]["release"] == "42"
    assert {
        "class": "simulation",
        "job_type": "struct_relax",
        "state": "succeeded",
        "count": 1,
    } in data["Jobs"]
    assert sum(row["count"] for row in data["Open gripe ages (current snapshot)"]) == 1
    assert len(data["Open gripes"]) == 1


def test_origin_goal_rows_optional(store: Store) -> None:
    ref_id = _gripe(store, LO, closed=True)
    assert collect(store, LO, HI)["Gripes by origin and goal"] == [
        {"note": "origin/goal not stamped yet"}
    ]
    for namespace in ("origin-class", "goal"):
        store.add_tag(ref_id, Tag.open(f"{namespace}:test"), set_by="agent")
    rows = collect(store, LO, HI)["Gripes by origin and goal"]
    assert len(rows) == 4
    assert all(row["count"] == 1 for row in rows)


def test_active_quest_counts_tick_resumes_once(store: Store) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="Active report quest")
    store.add_tag(quest.id, Tag.closed("STATUS", "active"), set_by="system")
    job_id = _job(
        store, "quest_tick", LO - timedelta(days=10), params={"quest_id": quest.id}
    )
    with store.tx() as conn:
        store.chunks.insert_chunks(
            job_id,
            [
                ChunkInsert(
                    ord=0, text="tick #1: progress", meta={"chunk_kind": "job_event"}
                ),
                ChunkInsert(
                    ord=1,
                    text="tick #1: awaiting sims",
                    meta={"chunk_kind": "job_event"},
                ),
                ChunkInsert(
                    ord=2, text="tick #2: progress", meta={"chunk_kind": "job_event"}
                ),
            ],
            conn=conn,
        )
        conn.execute("UPDATE chunks SET created_at=%s WHERE ref_id=%s", (LO, job_id))
    _job(store, "autocatpath_seed", LO, params={"quest_id": quest.id})
    _job(store, "struct_relax", HI, params={"quest_id": quest.id})
    assert collect(store, LO, HI)["Active quests"] == [
        {"quest": quest.id, "ticks": 2, "simulations": 1}
    ]


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError, match="since must be before until"):
        collect(MagicMock(), HI, LO)


def test_job_returns_tables_without_draft(store: Store) -> None:
    _job(store, "struct_relax", LO)
    ctx = MagicMock(
        store=store, meta={"params": {"since": LO.isoformat(), "until": HI.isoformat()}}
    )
    _dispatch(ctx, SPEC)
    ctx.record_failure.assert_not_called()
    kind, text = ctx.append_chunk.call_args.args
    assert kind == "job_summary"
    assert "## Jobs" in text and "struct_relax" in text and "simulation" in text
    assert "## Commits on main" in text


def test_section_tables_replaced_prose_kept(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = store.insert_ref(kind="todo", slug=None, title="weekly project")
    ref, title = store.drafts.create_draft(
        name="weekly-test", title="Weekly", project_ref_id=project.id
    )
    heading = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Numbers", at={"after": title.dc}
    )[0]
    prose = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="Human prose survives.",
        at={"into": heading.dc},
    )[0]
    nested = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Nested", at={"into": heading.dc}
    )[0]
    nested_table = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="table", text="nested table", at={"into": nested.dc}
    )[0]
    for _ in range(2):
        write_tables(store, "weekly-test", heading.dc, {"Jobs": [{"count": 3}]})
    chunks = store.drafts.reading_order(ref.id)
    tables = [
        c
        for c in chunks
        if c.chunk_kind == "table" and c.parent_chunk_id == heading.chunk_id
    ]
    assert len(tables) == 1
    assert tables[0].meta["table"]["rows"] == [[3]]
    assert (
        next(c for c in chunks if c.chunk_id == prose.chunk_id).text
        == "Human prose survives."
    )
    assert any(c.chunk_id == nested_table.chunk_id for c in chunks)

    # A failed rerun must roll retirement and insertion back together.
    def fail_insert(**kwargs: Any) -> None:
        raise RuntimeError("injected insertion failure")

    monkeypatch.setattr(store.drafts, "add_chunks", fail_insert)
    with pytest.raises(RuntimeError, match="injected insertion failure"):
        write_tables(store, "weekly-test", heading.dc, {"Jobs": [{"count": 99}]})
    assert [c.chunk_id for c in store.drafts.reading_order(ref.id)] == [
        c.chunk_id for c in chunks
    ]


def _git(path: Path, *args: str, when: datetime = LO) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    subprocess.run(
        ["git", "-C", str(path), *args],
        env=env,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


def test_git_section_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "fix(report): count gr592750",
        "-m",
        "Also gr592750 and gr123",
    )
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "feat: outside",
        when=HI,
    )
    _git(tmp_path, "checkout", "-b", "other")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "feat: off-main",
    )
    monkeypatch.setenv("PRECIS_WEEKLY_REPORT_CHECKOUT", str(tmp_path))
    assert git_rows(LO, HI) == [
        {"type": "fix", "scope": "report", "commits": 1, "gripes": "gr123, gr592750"}
    ]


def test_git_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_WEEKLY_REPORT_CHECKOUT", raising=False)
    assert git_rows(LO, HI) == [{"note": "git unavailable"}]
    assert "git unavailable" in render_markdown({"Git": git_rows(LO, HI)})


def test_weekly_cadence_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.workers.job_types import get_job_type
    from precis.workers.registry import SERVICES_BY_NAME
    from precis.workers.scheduler import CADENCES

    monkeypatch.delenv("PRECIS_WEEKLY_REPORT_ENABLED", raising=False)
    cadence = next(c for c in CADENCES if c.name == "weekly_report")
    assert cadence.interval_s == 7 * 24 * 3600
    assert cadence.eligible is not None and not cadence.eligible()
    assert get_job_type("weekly_report") is SPEC
    assert "weekly_report" in SERVICES_BY_NAME
